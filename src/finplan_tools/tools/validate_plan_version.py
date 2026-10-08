"""``validate_plan_version`` (spec plan-tools; task 7.4).

Triggers the platform's deterministic validation (``POST /v1/plan-versions/{id}/validate``, derived
idempotency key) and returns the resulting status (``validated`` or ``invalid``) with the platform's
itemized findings unchanged. The tool neither changes validation rules nor overrides an ``invalid``
result. Phase 1: only plans of synthetic portfolios (:mod:`._plan_guard`).
"""

from __future__ import annotations

from typing import Any

from ..core.errors import ToolError
from ..core.registry import register_tool
from ._common import producer_doc, project
from ._plan_guard import require_synthetic_plan

RESPONSE = "tools/validate-plan-version-response"


@register_tool(
    "validate_plan_version",
    description=(
        "Run the platform's deterministic validation on one plan version and return its status "
        "(validated or invalid) with itemized findings, unchanged. Requires an idempotency_key."
    ),
)
def validate_plan_version(ctx: Any, request: dict[str, Any]) -> dict[str, Any]:
    pv = request["plan_version_id"]
    version = producer_doc(producer_doc(ctx.platform.get_plan_version(pv, ctx.meta), "plan version").get("plan_version"), "plan version")
    require_synthetic_plan(ctx, str(version.get("plan_id")))
    result = producer_doc(ctx.platform.validate_plan_version(pv, ctx.downstream_body(request, drop=("plan_version_id",)), ctx.meta), "validation result")
    if result.get("plan_version_id") != pv:
        raise ToolError.internal("the platform validated another plan version than requested")
    doc = project(result, RESPONSE)
    if request.get("synthetic") is True:
        doc["synthetic"] = True
    return doc
