"""Invocation-source resolution and caller identity (spec tool-request-handling, D3).

Lambda direct invocation does not expose the invoking IAM identity to the function (LA-3). Trust
therefore comes from the Lambda resource policy (only the environment's single direct-test
principal, this repo's pipeline test/smoke roles and, once published, the Gateway principal may
invoke). The invocation *source* only selects the identity class:

=================  ======================  =====================  ==============================
source             how it is recognised    identity               caller-block channel
=================  ======================  =====================  ==============================
Gateway            Gateway client context  ``gateway:<env>``      ``hosted_agent``
direct test        ``source: direct_test`` ``direct:<env>``       ``direct_test``
pipeline suites    ``source: ci_test``     ``pipeline:<env>``     ``ci_test``
anything else      -                       ``UNAUTHORIZED``       -
=================  ======================  =====================  ==============================

Direct invocation event (documented in docs/direct-invocation.md)::

    {"finplan_invocation": {"source": "direct_test", "environment": "beta",
                            "correlation_id": "...", "contract_version": "1.0.0"},
     "tool": "get_plan_version",
     "arguments": {...the tool request...}}

A ``caller`` value anywhere in the event or at the top of ``arguments`` is never trusted: it is
removed before validation and recorded only as an untrusted, redacted annotation in the audit log.

Caller groups (``Invocation.groups``; add-approval-and-strategy-tools T2) come only from the trusted
invocation source, never from the request:

* ``direct_test`` (the environment's single direct-test principal, the project owner) and ``ci_test``
  (this repo's pipeline stage role) act for the project owner, who holds ``plan_publisher``. In prod
  neither may invoke a state-changing tool (Lambda grants and the pipeline's prod check).
* Gateway callers have no verified groups by default. The paid ``run_portfolio_research`` path
  verifies its transport-only Cognito access token with the pinned same-environment issuer/JWKS
  before replacing this aggregate identity with a hashed subject and signed groups. All other
  group-gated Gateway actions retain their existing fail-closed behavior.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import Any, Mapping

from .errors import ToolError
from .gateway import parse_gateway_call

__all__ = [
    "INVOCATION_KEY",
    "SOURCE_DIRECT_TEST",
    "SOURCE_CI_TEST",
    "SOURCE_GATEWAY",
    "Invocation",
    "resolve_invocation",
    "caller_block",
]

INVOCATION_KEY = "finplan_invocation"
SOURCE_DIRECT_TEST = "direct_test"
SOURCE_CI_TEST = "ci_test"
SOURCE_GATEWAY = "gateway"
_CLASSES = {
    SOURCE_DIRECT_TEST: ("direct", "direct_test"),
    SOURCE_CI_TEST: ("pipeline", "ci_test"),
    SOURCE_GATEWAY: ("gateway", "hosted_agent"),
}
#: Groups each trusted direct source acts with (see the module docstring).
_SOURCE_GROUPS = {
    SOURCE_DIRECT_TEST: frozenset({"plan_publisher"}),
    SOURCE_CI_TEST: frozenset({"plan_publisher"}),
}
_ALLOWED_ENVELOPE_KEYS = {INVOCATION_KEY, "tool", "arguments", "caller"}
_ALLOWED_INVOCATION_KEYS = {"source", "environment", "correlation_id", "contract_version", "caller"}
_SAFE = re.compile(r"^[A-Za-z0-9_.:@-]{1,64}\Z")


def _annotation(value: Any) -> str:
    """An untrusted value, logged only as a short safe token or ``<redacted>``."""
    return value if isinstance(value, str) and _SAFE.match(value) and not value.startswith("arn") else "<redacted>"


@dataclass(frozen=True)
class Invocation:
    """The resolved invocation: trusted identity class plus the (untrusted) request parts."""

    source: str
    identity: str
    channel: str
    environment: str
    arguments: Any
    tool_name: str | None = None
    declared_environment: str | None = None
    correlation_hint: str | None = None
    declared_contract_version: Any = None
    untrusted: dict[str, str] = field(default_factory=dict)
    #: Verified caller groups (Cognito-group names); empty when the source carries none.
    groups: frozenset[str] = frozenset()


def resolve_invocation(event: Any, context: Any, *, environment: str) -> Invocation:
    """Resolve the invocation source of a Lambda event; raises ``UNAUTHORIZED`` for unknown sources.

    ``environment`` is the Lambda's own environment (deployment configuration); identities are
    always built from it, never from a declared value.
    """
    untrusted: dict[str, str] = {}
    gw = parse_gateway_call(event, context)
    if gw is not None:
        args = gw.arguments
        if isinstance(args, Mapping) and "caller" in args:
            untrusted["caller"] = _annotation(args["caller"])
            args = {k: v for k, v in args.items() if k != "caller"}
        prefix, channel = _CLASSES[SOURCE_GATEWAY]
        return Invocation(SOURCE_GATEWAY, f"{prefix}:{environment}", channel, environment, args, gw.tool_name, untrusted=untrusted, groups=gw.caller_groups)

    if not isinstance(event, Mapping) or not isinstance(event.get(INVOCATION_KEY), Mapping):
        raise ToolError.unauthorized("the invocation carries neither Gateway context nor a configured direct-test marker")
    meta = event[INVOCATION_KEY]
    source = meta.get("source")
    if source not in (SOURCE_DIRECT_TEST, SOURCE_CI_TEST):
        raise ToolError.unauthorized("the invocation source is not recognised")
    unknown = sorted(set(event) - _ALLOWED_ENVELOPE_KEYS) + sorted(f"{INVOCATION_KEY}.{k}" for k in set(meta) - _ALLOWED_INVOCATION_KEYS)
    if unknown:
        raise ToolError.validation("unknown invocation envelope field", pointer="", fields=[_annotation(u) for u in unknown])
    for holder in (event, meta):
        if "caller" in holder:
            untrusted["caller"] = _annotation(holder["caller"])
    args = event.get("arguments", {})
    if isinstance(args, Mapping) and "caller" in args:
        untrusted["caller"] = _annotation(args["caller"])
        args = {k: v for k, v in args.items() if k != "caller"}
    declared_env = meta.get("environment")
    tool = event.get("tool")
    prefix, channel = _CLASSES[source]
    return Invocation(
        source=source,
        identity=f"{prefix}:{environment}",
        channel=channel,
        environment=environment,
        arguments=args,
        tool_name=tool if isinstance(tool, str) else None,
        declared_environment=declared_env if declared_env is not None else None,
        correlation_hint=meta.get("correlation_id") if isinstance(meta.get("correlation_id"), str) else None,
        declared_contract_version=meta.get("contract_version"),
        untrusted=untrusted,
        groups=_SOURCE_GROUPS[source],
    )


def caller_block(inv: Invocation, correlation_id: str, *, synthetic: bool = False) -> dict[str, Any]:
    """The contract ``core/v1/caller.json`` on-behalf-of block forwarded to producers (D3)."""
    block: dict[str, Any] = {
        "channel": inv.channel,
        "correlation_id": correlation_id,
        "subject_hash": "sha256:" + hashlib.sha256(inv.identity.encode("utf-8")).hexdigest(),
    }
    if synthetic:
        block["synthetic"] = True
    return block
