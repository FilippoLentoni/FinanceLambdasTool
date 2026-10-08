"""Structured audit log: exactly one record per invocation (spec "Structured audit logging", TRH-12).

Fields: ``correlation_id``, ``tool``, ``caller_identity``, ``channel``, ``environment``,
``contract_version``, ``release_id``, ``outcome`` (``OK`` or the error code), ``retryable``,
``downstream_ids`` and ``duration_ms``, plus ``untrusted`` annotations (for example a ``caller``
value a request asserted, already reduced to a safe token or ``<redacted>``).

Never logged: request or response payloads, download grants, idempotency keys (raw or derived),
secrets or storage locations. ``downstream_ids`` keeps only contract identifier fields whose values
match the contract identifier patterns, so nothing else can leak through it.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Mapping

__all__ = ["AUDIT_LOGGER", "IDENTIFIER_FIELDS", "audit_record", "emit", "collect_ids"]

AUDIT_LOGGER = "finplan_tools.audit"
_log = logging.getLogger(AUDIT_LOGGER)

_ULID = r"[0-7][0-9A-HJKMNP-TV-Z]{25}"
IDENTIFIER_FIELDS: dict[str, re.Pattern[str]] = {
    "plan_id": re.compile(rf"^pl_{_ULID}\Z"),
    "plan_version_id": re.compile(rf"^pv_{_ULID}\Z"),
    "parent_plan_version_id": re.compile(rf"^pv_{_ULID}\Z"),
    "portfolio_id": re.compile(rf"^pf_{_ULID}\Z"),
    "publication_id": re.compile(rf"^pub_{_ULID}\Z"),
    "input_snapshot_id": re.compile(rf"^snap_{_ULID}\Z"),
    "run_id": re.compile(rf"^run_{_ULID}\Z"),
    "model_version": re.compile(rf"^mv_{_ULID}\Z"),
    "configuration_id": re.compile(r"^cfg_[0-9a-f]{64}\Z"),
}
_MAX_IDS_PER_FIELD = 5


def collect_ids(*docs: Any) -> dict[str, list[str]]:
    """Contract identifiers found (shallowly and one level deep) in request/response documents."""
    found: dict[str, list[str]] = {}

    def take(d: Mapping[str, Any]) -> None:
        for k, pat in IDENTIFIER_FIELDS.items():
            v = d.get(k)
            if isinstance(v, str) and pat.match(v):
                lst = found.setdefault(k, [])
                if v not in lst and len(lst) < _MAX_IDS_PER_FIELD:
                    lst.append(v)

    for doc in docs:
        if isinstance(doc, Mapping):
            take(doc)
            for v in doc.values():
                if isinstance(v, Mapping):
                    take(v)
    return found


def audit_record(
    *,
    correlation_id: str,
    tool: str | None,
    caller_identity: str | None,
    channel: str | None,
    environment: str,
    contract_version: str,
    release_id: str | None,
    outcome: str,
    retryable: bool | None = None,
    downstream_ids: Mapping[str, list[str]] | None = None,
    duration_ms: float | None = None,
    untrusted: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    rec: dict[str, Any] = {
        "event": "tool_invocation",
        "correlation_id": correlation_id,
        "tool": tool,
        "caller_identity": caller_identity,
        "channel": channel,
        "environment": environment,
        "contract_version": contract_version,
        "release_id": release_id,
        "outcome": outcome,
        "downstream_ids": {k: list(v) for k, v in (downstream_ids or {}).items()},
    }
    if retryable is not None:
        rec["retryable"] = retryable
    if duration_ms is not None:
        rec["duration_ms"] = round(duration_ms, 1)
    if untrusted:
        rec["untrusted"] = dict(untrusted)
    return rec


def emit(record: Mapping[str, Any]) -> None:
    _log.info(json.dumps(record, sort_keys=True, separators=(",", ":")))
