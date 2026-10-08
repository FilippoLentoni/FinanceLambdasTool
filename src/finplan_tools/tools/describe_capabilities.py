"""``describe_capabilities`` (spec capability-discovery; tasks 4.1, 4.2).

Lists every catalog tool with its schema ``$id``s, kind and availability in this environment, the
release and the pinned contract version. Availability is computed from the producers' release
manifests in the same environment (never hard-coded): an absent manifest gives
``DEPENDENCY_UNAVAILABLE``, a manifest that does not serve the pinned major gives
``UNSUPPORTED_CONTRACT_VERSION``.

Declared limits (``limits``) come only from configuration and declarations:

* ``response_max_bytes`` and ``max_estimated_usd_per_call`` from ``tool-limits``;
* ``market_data_granularities`` from the granularities the pinned contract's
  ``refresh_market_data`` request declares (the platform's provider capability declaration in the
  contract; ``intraday`` is never inferred from ``daily``);
* ``experiment_types``: FinanceModel publishes no job-type declaration in its release manifest or SSM
  (CONTRACT GAP), so it is reported as not declared rather than guessed.

Read-only: no idempotency key, no producer call, no endpoint, ARN, account or role name.
"""

from __future__ import annotations

from typing import Any

from ..core.compat import producer_availability
from ..core.contracts import contract_version, served_majors, store
from ..core.errors import ToolError
from ..core.registry import CATALOG, get_tool, register_tool
from ._common import iso

GPU_APPROVAL_NOTE = "GPU runs always wait in awaiting_approval for explicit user approval in FinanceModel; no tool can approve a run."


def declared_granularities() -> list[str]:
    """Granularities the pinned ``refresh_market_data`` request schema declares."""
    schema = store().get("tools/refresh-market-data-request").schema
    return list(schema["properties"]["granularity"].get("enum") or [])


def tool_entries(ctx: Any) -> list[dict[str, Any]]:
    manifests: dict[tuple[str, str | None], tuple[bool, str | None]] = {}
    out = []
    for name, entry in CATALOG.items():
        available, reason = True, None
        for producer in entry.producers:
            key = (producer, entry.min_producer_contract)
            if key not in manifests:
                manifests[key] = producer_availability(ctx.manifest(producer), entry.min_producer_contract)
            ok, why = manifests[key]
            if not ok:
                available, reason = False, why
                break
        if available and get_tool(name) is None:
            available, reason = False, "DEPENDENCY_UNAVAILABLE"
        item: dict[str, Any] = {
            "name": name,
            "input_schema_id": store().get(entry.input_schema).schema["$id"],
            "output_schema_id": store().get(entry.output_schema).schema["$id"],
            "state_changing": entry.state_changing,
            "available": available,
        }
        if not available:
            item["unavailable_reason"] = reason
        out.append(item)
    return out


@register_tool(
    "describe_capabilities",
    description=(
        "List the tools of this environment's release with their input/output schema ids, whether each is "
        "read-only or state-changing and whether its producer service is available, plus the release, the "
        "pinned contract version and the declared limits (response size, per-call budget limits by budget "
        "category, market-data granularities). Read-only."
    ),
    gate_dependencies=False,
)
def describe_capabilities(ctx: Any, request: dict[str, Any]) -> dict[str, Any]:
    if not ctx.release_id:
        raise ToolError.internal("this function has no release id configured", reason="release_id_missing")
    limits = ctx.limits
    doc: dict[str, Any] = {
        "environment": ctx.environment,
        "release_id": ctx.release_id,
        "contract_version": contract_version(),
        "served_contract_majors": served_majors(),
        "tools": tool_entries(ctx),
        "generated_at": iso(ctx.now()),
        "limits": {
            "response_max_bytes": limits.response_max_bytes,
            "max_estimated_usd_per_call": dict(sorted(limits.max_estimated_usd_per_call.items())),
            "max_estimated_usd_per_call_source": "tool-limits",
            "unlisted_budget_category": "rejected",
            "gpu_runs_require_user_approval": True,
            "approval_note": GPU_APPROVAL_NOTE,
            "page_size_max": limits.page_size_max,
            "market_data_granularities": declared_granularities(),
            "market_data_granularities_source": "contract:refresh_market_data request",
            "experiment_types": [],
            "experiment_types_declared": False,
        },
    }
    if ctx.synthetic:
        doc["synthetic"] = True
    return doc
