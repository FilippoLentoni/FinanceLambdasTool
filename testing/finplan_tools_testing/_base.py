"""Shared machinery of the mock producers: routing, idempotency, faults, call log, ID minting."""

from __future__ import annotations

import copy
import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Mapping

from finplan_contracts.canonical import request_hash
from finplan_contracts.schemas import contracts_root

from finplan_tools.core.contracts import validate_document
from finplan_tools.core.errors import ToolError
from finplan_tools.core.transport import HEADER_CALLER, HEADER_CORRELATION, ProducerRequest, ProducerResponse

_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
HTTP_STATUS = {
    "VALIDATION_FAILED": 400,
    "INVALID_IDENTIFIER": 400,
    "UNSUPPORTED_CONTRACT_VERSION": 400,
    "UNAUTHORIZED": 401,
    "FORBIDDEN": 403,
    "OPERATION_NOT_PERMITTED": 403,
    "BUDGET_EXCEEDED": 403,
    "NOT_FOUND": 404,
    "CONFLICT": 409,
    "IMMUTABLE_RECORD": 409,
    "IDEMPOTENCY_KEY_REUSED": 422,
    "PRECONDITION_FAILED": 422,
    "RATE_LIMITED": 429,
    "INTERNAL": 500,
    "DEPENDENCY_UNAVAILABLE": 503,
}


def fixture(name: str, file: str) -> dict[str, Any]:
    """A *valid* fixture of the pinned contract package (``fixtures/<name>/valid/<file>.json``)."""
    path = contracts_root() / "fixtures" / name / "valid" / f"{file}.json"
    return json.loads(path.read_text(encoding="utf-8"))


class MockIds:
    """Deterministic, ULID-shaped identifiers (the mocks play the producers, which mint IDs)."""

    def __init__(self, seed: int = 0) -> None:
        self.n = seed

    def ulid(self) -> str:
        self.n += 1
        n, out = self.n, ""
        while n:
            n, r = divmod(n, 32)
            out = _CROCKFORD[r] + out
        return "01KM" + out.rjust(22, "0")

    def mint(self, prefix: str) -> str:
        return f"{prefix}_{self.ulid()}"


class Clock:
    def __init__(self, start: str = "2026-01-12T14:30:00Z") -> None:
        self.t = datetime.fromisoformat(start.replace("Z", "+00:00"))

    def now(self) -> datetime:
        return self.t

    def iso(self) -> str:
        return self.t.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    def advance(self, seconds: float) -> None:
        self.t += timedelta(seconds=seconds)


def require_valid(doc: Any, schema: str) -> Any:
    """Mocks only ever answer contract-conformant documents (producer-mode conformance)."""
    res = validate_document(doc, schema)
    if not res.valid:
        raise AssertionError(f"mock produced a non-conformant {schema}: {[i.message for i in res.issues][:5]}")
    return doc


@dataclass
class Fault:
    status: int
    body: Any
    remaining: int = 1


@dataclass
class Call:
    op: str
    method: str
    path: str
    query: dict[str, Any]
    body: Any
    headers: dict[str, str]


@dataclass
class Route:
    method: str
    pattern: re.Pattern[str]
    op: str
    fn: Callable[..., tuple[int, Any]]


class MockProducer:
    """Base class: a :class:`~finplan_tools.core.transport.Transport` answering from routes."""

    producer = "mock"

    def __init__(self, *, clock: Clock | None = None, ids: MockIds | None = None) -> None:
        self.clock = clock or Clock()
        self.ids = ids or MockIds()
        self.calls: list[Call] = []
        self.faults: dict[str, list[Fault]] = {}
        self.unreachable = False
        self.missing_routes: set[str] = set()
        self.idempotency: dict[tuple[str, str], tuple[str, int, Any]] = {}
        self.routes: list[Route] = []

    # ------------------------------------------------------------ test controls
    def count(self, op: str | None = None) -> int:
        return sum(1 for c in self.calls if op is None or c.op == op)

    def ops(self) -> list[str]:
        return [c.op for c in self.calls]

    def fail_next(self, op: str, code: str, *, times: int = 1, retryable: bool | None = None, status: int | None = None, details: Mapping[str, Any] | None = None, raw_body: Any = ...) -> None:
        """Make the next ``times`` calls of ``op`` answer an error (contract envelope by default)."""
        if raw_body is ...:
            body: Any = ToolError(code, f"injected {code}", retryable=retryable, details=details).to_envelope("mock-fault-0001")
        else:
            body = raw_body
        self.faults.setdefault(op, []).append(Fault(status or HTTP_STATUS.get(code, 500), body, times))

    def route(self, method: str, template: str, op: str, fn: Callable[..., tuple[int, Any]]) -> None:
        rx = "^" + re.sub(r"\{(\w+)\}", r"(?P<\1>[^/]+)", template.strip("/")) + r"/?\Z"
        self.routes.append(Route(method, re.compile(rx), op, fn))

    # ------------------------------------------------------------ helpers
    def error(self, code: str, message: str, **details: Any) -> tuple[int, Any]:
        return HTTP_STATUS[code], ToolError(code, message, details=details).to_envelope("mock-producer-0001")

    def idempotent(self, op: str, body: Mapping[str, Any], fn: Callable[[], tuple[int, Any]]) -> tuple[int, Any]:
        """Producer idempotency: same key + same body -> replay; same key + other body -> REUSED."""
        key = body.get("idempotency_key")
        if not isinstance(key, str):
            return self.error("VALIDATION_FAILED", "idempotency_key is required", pointer="/idempotency_key")
        h = request_hash(dict(body))
        rec = self.idempotency.get((op, key))
        if rec is not None:
            if rec[0] != h:
                return self.error("IDEMPOTENCY_KEY_REUSED", "the idempotency key was used with a different request")
            return rec[1], copy.deepcopy(rec[2])
        status, resp = fn()
        if 200 <= status < 300:
            self.idempotency[(op, key)] = (h, status, copy.deepcopy(resp))
        return status, resp

    # ------------------------------------------------------------ transport
    def send(self, request: ProducerRequest) -> ProducerResponse:
        path = request.path.strip("/")
        match = next(((r, m) for r in self.routes if r.method == request.method.upper() and (m := r.pattern.match(path))), None)
        op = match[0].op if match else "unrouted"
        headers = dict(request.headers)
        self.calls.append(Call(op, request.method.upper(), path, dict(request.query), copy.deepcopy(request.body), headers))
        if self.unreachable:
            raise ToolError.dependency_unavailable(f"the {self.producer} API is unreachable", producer=self.producer, reason="mock_unreachable")
        if HEADER_CORRELATION not in headers:
            raise AssertionError("producer call without X-Correlation-Id")
        if HEADER_CALLER in headers:
            block = json.loads(headers[HEADER_CALLER])
            require_valid(block, "caller")
        faults = self.faults.get(op) or []
        if faults:
            f = faults[0]
            f.remaining -= 1
            if f.remaining <= 0:
                faults.pop(0)
            return ProducerResponse(f.status, copy.deepcopy(f.body), {"x-correlation-id": headers[HEADER_CORRELATION]})
        if match is None or op in self.missing_routes:
            status, body = self.error("NOT_FOUND", "no such route", record_type="route")
        else:
            route, m = match
            status, body = route.fn(request=request, **m.groupdict())
        return ProducerResponse(status, copy.deepcopy(body), {"x-correlation-id": headers[HEADER_CORRELATION]})


__all__ = ["Call", "Clock", "Fault", "HTTP_STATUS", "MockIds", "MockProducer", "fixture", "require_valid"]
