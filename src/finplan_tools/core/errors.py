"""Tool errors in the contract error vocabulary (spec tool-request-handling, "Error envelope mapping").

Every failure is a :class:`ToolError` carrying a registered code from the pinned
``core/v1/error-codes.json``. ``retryable`` defaults to the registry value; codes whose value is
fixed by the contract cannot be overridden. :meth:`ToolError.to_envelope` renders the
``core/v1/error.json`` envelope. Messages and details never carry storage locations, ARNs,
secrets or stack traces: :func:`sanitize_details` drops any string value that looks like one,
and the pipeline validates every envelope with the contract ``no_leaks`` check before returning.

Producer errors (platform or FinanceModel envelopes) keep their code and ``retryable`` flag
(:func:`from_producer_envelope`); throttling becomes ``RATE_LIMITED``, unreachable or undeployed
producers ``DEPENDENCY_UNAVAILABLE``, anything else ``INTERNAL``.
"""

from __future__ import annotations

import re
from typing import Any, Mapping

from finplan_contracts.validate import ValidationResult

from .contracts import contract_version, registered_error_codes

__all__ = [
    "ToolError",
    "from_validation",
    "from_producer_envelope",
    "from_http_status",
    "sanitize_details",
    "VALIDATION_FAILED",
    "INVALID_IDENTIFIER",
    "NOT_FOUND",
    "CONFLICT",
    "IDEMPOTENCY_KEY_REUSED",
    "IMMUTABLE_RECORD",
    "PRECONDITION_FAILED",
    "UNAUTHORIZED",
    "FORBIDDEN",
    "OPERATION_NOT_PERMITTED",
    "BUDGET_EXCEEDED",
    "RATE_LIMITED",
    "DEPENDENCY_UNAVAILABLE",
    "UNSUPPORTED_CONTRACT_VERSION",
    "INTERNAL",
]

VALIDATION_FAILED = "VALIDATION_FAILED"
INVALID_IDENTIFIER = "INVALID_IDENTIFIER"
NOT_FOUND = "NOT_FOUND"
CONFLICT = "CONFLICT"
IDEMPOTENCY_KEY_REUSED = "IDEMPOTENCY_KEY_REUSED"
IMMUTABLE_RECORD = "IMMUTABLE_RECORD"
PRECONDITION_FAILED = "PRECONDITION_FAILED"
UNAUTHORIZED = "UNAUTHORIZED"
FORBIDDEN = "FORBIDDEN"
OPERATION_NOT_PERMITTED = "OPERATION_NOT_PERMITTED"
BUDGET_EXCEEDED = "BUDGET_EXCEEDED"
RATE_LIMITED = "RATE_LIMITED"
DEPENDENCY_UNAVAILABLE = "DEPENDENCY_UNAVAILABLE"
UNSUPPORTED_CONTRACT_VERSION = "UNSUPPORTED_CONTRACT_VERSION"
INTERNAL = "INTERNAL"

#: Strings that must never leave a tool: storage URIs, ARNs, AWS endpoints, account IDs and
#: tracebacks. (JSON pointers such as ``/configuration/payload`` are legitimate details.)
_LEAKY = re.compile(
    r"(?i)(\b(s3a?|s3n|gs|hdfs|file|ftp)://|\barn:[a-z0-9-]*:|amazonaws\.com|\b[0-9]{12}\b|Traceback \(most recent call last\)|File \"[^\"]+\", line [0-9]+)"
)


def default_retryable(code: str) -> bool:
    return bool(registered_error_codes()[code]["retryable"])


def sanitize_details(value: Any, _depth: int = 0) -> Any:
    """Copy of ``value`` with leak-prone strings replaced by ``<redacted>`` (bounded depth)."""
    if _depth > 8:
        return "<redacted>"
    if isinstance(value, str):
        return "<redacted>" if _LEAKY.search(value) else value[:500]
    if isinstance(value, Mapping):
        return {str(k)[:64]: sanitize_details(v, _depth + 1) for k, v in list(value.items())[:50]}
    if isinstance(value, (list, tuple)):
        return [sanitize_details(v, _depth + 1) for v in list(value)[:50]]
    if isinstance(value, (bool, int, float)) or value is None:
        return value
    return "<redacted>"


class ToolError(Exception):
    """A failure reported to the caller as a contract error envelope."""

    def __init__(self, code: str, message: str, *, retryable: bool | None = None, details: Mapping[str, Any] | None = None, **detail_kw: Any) -> None:
        codes = registered_error_codes()
        if code not in codes:
            raise ValueError(f"unregistered error code {code!r}")
        reg = codes[code]
        if retryable is None or reg.get("retryable_fixed"):
            retryable = bool(reg["retryable"])
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = (message or code)[:2000]
        self.retryable = bool(retryable)
        self.details: dict[str, Any] = {**dict(details or {}), **detail_kw}

    # ------------------------------------------------------------------ helpers
    @classmethod
    def validation(cls, message: str, pointer: str = "", **details: Any) -> "ToolError":
        return cls(VALIDATION_FAILED, message, pointer=pointer, **details)

    @classmethod
    def invalid_identifier(cls, field: str, message: str | None = None, **details: Any) -> "ToolError":
        return cls(INVALID_IDENTIFIER, message or f"invalid identifier in field '{field}'", field=field, **details)

    @classmethod
    def forbidden(cls, message: str, **details: Any) -> "ToolError":
        return cls(FORBIDDEN, message, **details)

    @classmethod
    def unauthorized(cls, message: str = "unknown invocation source", **details: Any) -> "ToolError":
        return cls(UNAUTHORIZED, message, **details)

    @classmethod
    def dependency_unavailable(cls, message: str, **details: Any) -> "ToolError":
        return cls(DEPENDENCY_UNAVAILABLE, message, **details)

    @classmethod
    def precondition(cls, message: str, **details: Any) -> "ToolError":
        return cls(PRECONDITION_FAILED, message, **details)

    @classmethod
    def internal(cls, message: str = "unexpected failure", **details: Any) -> "ToolError":
        return cls(INTERNAL, message, **details)

    @classmethod
    def unsupported_contract_version(cls, served: list[int], declared: Any = None) -> "ToolError":
        # ``served_contract_majors`` is required by core/v1/error.json; ``served_majors`` is the
        # name the tool-request-handling spec uses. Both carry the same list.
        details: dict[str, Any] = {"served_contract_majors": list(served), "served_majors": list(served)}
        if isinstance(declared, int) and not isinstance(declared, bool):
            details["declared_major"] = declared
        return cls(UNSUPPORTED_CONTRACT_VERSION, "the declared contract major is not served by this release", details=details)

    # ------------------------------------------------------------------ envelope
    def to_envelope(self, correlation_id: str, *, synthetic: bool = False) -> dict[str, Any]:
        env: dict[str, Any] = {
            "code": self.code,
            "message": self.message,
            "retryable": self.retryable,
            "details": sanitize_details(self.details),
            "correlation_id": correlation_id,
            "contract_version": contract_version(),
        }
        if synthetic:
            env["synthetic"] = True
        return env


def from_validation(result: ValidationResult) -> ToolError:
    """Map an invalid contract validation result to a :class:`ToolError` (pointer/field kept)."""
    env = result.to_error_envelope(correlation_id="placeholder-0000", contract_version=contract_version())
    return ToolError(env["code"], env["message"], details=env["details"])


def from_producer_envelope(body: Any, status: int | None = None) -> ToolError:
    """A producer error passed through: same code and ``retryable``; unknown shapes -> INTERNAL."""
    codes = registered_error_codes()
    if isinstance(body, Mapping) and body.get("code") in codes:
        code = str(body["code"])
        retry = body.get("retryable")
        details = body.get("details") if isinstance(body.get("details"), Mapping) else {}
        msg = body.get("message") if isinstance(body.get("message"), str) and not _LEAKY.search(body["message"]) else f"producer returned {code}"
        return ToolError(code, msg, retryable=retry if isinstance(retry, bool) else None, details=details)
    return from_http_status(status)


def from_http_status(status: int | None) -> ToolError:
    """A producer response without a contract envelope, classified by HTTP status only."""
    if status == 429:
        return ToolError(RATE_LIMITED, "the producer throttled the request", retryable=True)
    if status in (502, 503, 504):
        return ToolError(DEPENDENCY_UNAVAILABLE, "the producer is unavailable", retryable=True)
    if status == 403:
        return ToolError(FORBIDDEN, "the producer refused the request")
    if status == 401:
        return ToolError(UNAUTHORIZED, "the producer could not authenticate the request")
    return ToolError(INTERNAL, "unexpected producer response", **({"producer_status": status} if isinstance(status, int) else {}))
