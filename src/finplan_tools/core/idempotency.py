"""Derived downstream idempotency keys and deterministic downstream bodies (D4, TRH-06, ID-10).

The tool owns no idempotency store. It forwards::

    "lt_" + lowercase hex SHA-256( "caller_identity|env|tool|idempotency_key" )

computed by the contract package (``finplan_contracts.keys.derive_proxied_key``), together with a
downstream body that is a pure function of the tool request: no timestamps, correlation IDs or
caller blocks in the body (those travel in transport headers). The producers' 7-day idempotency
records then return the original result for a retry and ``IDEMPOTENCY_KEY_REUSED`` for the same
key with a different body.
"""

from __future__ import annotations

import copy
from typing import Any, Iterable, Mapping

from finplan_contracts.canonical import canonicalize, request_hash
from finplan_contracts.keys import DERIVED_KEY_PATTERN, derive_proxied_key

__all__ = ["derived_key", "downstream_body", "body_bytes", "body_hash", "DERIVED_KEY_PATTERN"]

#: Fields of a tool request that never go downstream (transport-level or tool-only).
TOOL_ONLY_FIELDS = frozenset({"contract_version"})


def derived_key(identity: str, environment: str, tool: str, idempotency_key: str) -> str:
    return derive_proxied_key(identity, environment, tool, idempotency_key)


def downstream_body(
    request: Mapping[str, Any],
    *,
    identity: str,
    environment: str,
    tool: str,
    drop: Iterable[str] = (),
    rename: Mapping[str, str] | None = None,
    extra: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Deterministic producer body for a write tool.

    Copies ``request`` without tool-only fields and ``drop``, renames keys per ``rename``, merges
    ``extra`` (deterministic values only) and replaces ``idempotency_key`` with the derived key.
    """
    if "idempotency_key" not in request:
        raise ValueError("write tools require an idempotency_key")
    skip = set(TOOL_ONLY_FIELDS) | set(drop)
    body = {(rename or {}).get(k, k): copy.deepcopy(v) for k, v in request.items() if k not in skip}
    body.update(copy.deepcopy(dict(extra or {})))
    body["idempotency_key"] = derived_key(identity, environment, tool, str(request["idempotency_key"]))
    return body


def body_bytes(body: Any) -> bytes:
    """RFC 8785 canonical bytes of a body (what the producers hash)."""
    return canonicalize(body)


def body_hash(body: Any) -> str:
    return request_hash(body)
