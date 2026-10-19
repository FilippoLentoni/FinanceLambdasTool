"""Helpers shared by the tool modules (not a tool: ``load_all`` skips ``_``-prefixed modules)."""

from __future__ import annotations

import copy
from datetime import datetime, timezone
from functools import lru_cache
from typing import Any, Iterable, Mapping

from ..core.contracts import store
from ..core.errors import ToolError
from ..core.public_response import public_document

__all__ = ["schema_properties", "project", "iso", "producer_doc", "require_requested_universe"]


def _props(schema: Mapping[str, Any]) -> set[str]:
    out = set((schema.get("properties") or {}).keys())
    for sub in schema.get("allOf") or ():
        ref = sub.get("$ref") if isinstance(sub, Mapping) else None
        if isinstance(ref, str):
            out |= schema_properties(ref.rsplit("/v1/", 1)[-1].removesuffix(".json"))
        elif isinstance(sub, Mapping):
            out |= _props(sub)
    return out


@lru_cache(maxsize=None)
def schema_properties(name: str) -> frozenset[str]:
    """Top-level property names a pinned contract schema declares (following ``allOf`` refs)."""
    return frozenset(_props(store().get(name).schema))


def project(doc: Mapping[str, Any], schema: str, *, keep: Iterable[str] = ()) -> dict[str, Any]:
    """Copy of ``doc`` with only the properties ``schema`` declares (plus ``keep``).

    Producer documents may carry fields a later producer release added; a tool returns only what its
    pinned output schema names, so nothing unreviewed (storage hints, internal state) reaches a caller.
    """
    allowed = schema_properties(schema) | set(keep)
    return {k: copy.deepcopy(v) for k, v in doc.items() if k in allowed}


def producer_doc(value: Any, what: str) -> dict[str, Any]:
    """A public copy of a producer 2xx object, preserving its immutable evidence joins."""
    if not isinstance(value, Mapping):
        raise ToolError.internal(f"the producer returned no {what} document")
    return public_document(value)


def require_requested_universe(doc: Mapping[str, Any], instruments: Iterable[str], *, strategy: str | None = None) -> None:
    """Refuse a producer recommendation that targets an instrument outside the requested
    ``instruments`` (or, when ``strategy`` is given, that another model produced): the
    ``generate_portfolio_recommendation_*`` tools return weights only for the caller's universe."""
    rec = doc.get("recommendation")
    if not isinstance(rec, Mapping):
        raise ToolError.internal("the producer returned no recommendation")
    if strategy is not None and rec.get("strategy") != strategy:
        raise ToolError.internal(f"the producer returned a recommendation of another model than {strategy}")
    allowed = set(instruments)
    targets = rec.get("target_weights")
    if not isinstance(targets, list):
        raise ToolError.internal("the producer recommendation has no target weights")
    outside = sorted({str(t.get("instrument_id")) for t in targets if not isinstance(t, Mapping) or t.get("instrument_id") not in allowed})
    if outside:
        raise ToolError.internal("the producer recommendation targets instruments outside the requested universe", instruments=outside[:20])


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
