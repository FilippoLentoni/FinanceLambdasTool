"""Producer transport interface (shared by the real SigV4 HTTP transport and the test-only mocks).

A :class:`Transport` sends one :class:`ProducerRequest` (method, path, query, JSON body, transport
headers) to one producer API and returns a :class:`ProducerResponse`. The typed clients in
``finplan_tools.backends`` build requests and map non-2xx responses to contract errors, so the same
client code runs against the deployed APIs (``backends.sigv4.SigV4HttpTransport``) and the
in-process mocks (``finplan_tools_testing``).

Transport headers (outside the hashed request body, so producer idempotency hashes stay stable):
``X-Correlation-Id``, ``X-Finplan-Contract-Version`` and ``X-Finplan-Caller`` (the contract
``core/v1/caller.json`` block).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Mapping, Protocol, runtime_checkable

__all__ = ["CallMeta", "ProducerRequest", "ProducerResponse", "Transport", "HEADER_CORRELATION", "HEADER_CONTRACT", "HEADER_CALLER"]

HEADER_CORRELATION = "X-Correlation-Id"
HEADER_CONTRACT = "X-Finplan-Contract-Version"
HEADER_CALLER = "X-Finplan-Caller"


@dataclass(frozen=True)
class CallMeta:
    """Per-invocation transport metadata (never part of a hashed body)."""

    correlation_id: str
    contract_version: str
    caller: Mapping[str, Any] | None = None

    def headers(self) -> dict[str, str]:
        h = {HEADER_CORRELATION: self.correlation_id, HEADER_CONTRACT: self.contract_version}
        if self.caller is not None:
            h[HEADER_CALLER] = json.dumps(dict(self.caller), sort_keys=True, separators=(",", ":"))
        return h


@dataclass(frozen=True)
class ProducerRequest:
    method: str
    path: str
    query: Mapping[str, Any] = field(default_factory=dict)
    body: Any = None
    headers: Mapping[str, str] = field(default_factory=dict)
    timeout_seconds: float = 15.0


@dataclass(frozen=True)
class ProducerResponse:
    status: int
    body: Any
    headers: Mapping[str, str] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300


@runtime_checkable
class Transport(Protocol):
    def send(self, request: ProducerRequest) -> ProducerResponse:
        """Send one request; raise ``ToolError(DEPENDENCY_UNAVAILABLE|RATE_LIMITED)`` on transport failure."""
        ...
