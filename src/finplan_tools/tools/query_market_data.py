"""``query_market_data`` (spec market-data-tools; task 5.3).

Reads one snapshot of this environment through the platform only:
``GET /v1/snapshots/{id}/observations`` (G-2) gives the snapshot record, observation kinds,
per-instrument summaries over the whole filtered range, ``partial`` and the trusted payload
reference. The tool returns the summaries, never the raw observation page (it asks the platform for
a one-row page), and never fills, interpolates or invents observations.

* Only ``approved`` snapshots are summarized; ``committed`` or ``expired`` ->
  ``PRECONDITION_FAILED`` with the snapshot ``status`` in ``details`` and no summary.
* Lineage (provider, provider library version, retrieval timestamp), quality flags and the dataset
  identity (``finance/etf-daily/<instrument>``) are returned exactly as the platform recorded them.
* ``partial`` is true when the requested range is not covered (or sessions are missing), with the
  real coverage and the platform's flags.
* A snapshot of another environment does not exist here: the platform answers ``NOT_FOUND``.
* A platform release without the observations route: metadata only (``GET /v1/snapshots/{id}``),
  with the observation summary reported unavailable (``DEPENDENCY_UNAVAILABLE``).

The platform filters instruments with the repeated query parameter ``instrument_id`` (its route
declares ``instrument_id`` as a list); dates with ``start_date`` / ``end_date``.
"""

from __future__ import annotations

from typing import Any

from ..core.compat import check_snapshot_compatibility
from ..core.errors import DEPENDENCY_UNAVAILABLE, ToolError
from ..core.registry import register_tool
from ._common import producer_doc

SUMMARY_UNAVAILABLE = {"available": False, "unavailable_reason": DEPENDENCY_UNAVAILABLE, "message": "the platform release in this environment does not serve observation reads; metadata only"}


def _payload_refs(snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    return [dict(a) for a in snapshot.get("artifacts") or () if isinstance(a, dict) and a.get("kind") == "snapshot_payload"]


def _instrument(summary: dict[str, Any]) -> dict[str, Any]:
    keep = ("instrument_id", "observation_count", "first_date", "last_date", "close_min", "close_max", "close_last")
    return {k: summary[k] for k in keep if k in summary}


@register_tool(
    "query_market_data",
    description=(
        "Read an approved market-data snapshot of this environment by input_snapshot_id, optionally "
        "filtered by instrument and date range: snapshot metadata (dataset, coverage, lineage, quality "
        "flags, manifest checksum), observation kinds, per-instrument summary statistics and trusted "
        "references to the full data. Reports partial coverage explicitly; never fills gaps. Read-only."
    ),
)
def query_market_data(ctx: Any, request: dict[str, Any]) -> dict[str, Any]:
    if request.get("next_token") is not None:
        ctx.unwrap_token(request["next_token"])  # only tokens this tool issued (it issues none today)
    sid = request["input_snapshot_id"]
    query: dict[str, Any] = {"page_size": 1}
    if request.get("instrument_ids"):
        query["instrument_id"] = list(request["instrument_ids"])
    for k in ("start_date", "end_date"):
        if request.get(k):
            query[k] = request[k]
    if query.get("start_date") and query.get("end_date") and query["start_date"] > query["end_date"]:
        raise ToolError.validation("start_date must not be after end_date", pointer="/start_date")
    try:
        page = producer_doc(ctx.platform.read_snapshot_observations(sid, ctx.meta, query), "observation summary")
    except ToolError as err:
        if err.code != DEPENDENCY_UNAVAILABLE or err.details.get("reason") != "route_not_deployed":
            raise
        page = None
    if page is None:
        snapshot = producer_doc(producer_doc(ctx.platform.get_snapshot(sid, ctx.meta), "snapshot").get("snapshot"), "snapshot")
        check_snapshot_compatibility(snapshot, require_approved=True)
        doc: dict[str, Any] = {
            "snapshot": snapshot,
            "observation_kinds": [],
            "instruments": [],
            "partial": True,
            "data_refs": _payload_refs(snapshot),
            "next_token": None,
            "observation_summary": dict(SUMMARY_UNAVAILABLE),
        }
    else:
        snapshot = producer_doc(page.get("snapshot"), "snapshot")
        check_snapshot_compatibility(snapshot, require_approved=True)
        doc = {
            "snapshot": snapshot,
            "observation_kinds": list(page.get("observation_kinds") or []),
            "instruments": [_instrument(s) for s in page.get("instruments") or () if isinstance(s, dict)],
            "partial": bool(page.get("partial")),
            "data_refs": [dict(r) for r in page.get("data_refs") or ()] or _payload_refs(snapshot),
            "next_token": None,
        }
        for k in ("requested_range", "uncovered_ranges"):
            if page.get(k) is not None:
                doc[k] = page[k]
        if page.get("missing_sessions"):
            doc["missing_session_count"] = len(page["missing_sessions"])
    if snapshot.get("synthetic") is True or request.get("synthetic") is True:
        doc["synthetic"] = True
    return doc
