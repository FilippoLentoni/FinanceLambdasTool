"""``publish_plan_version`` (spec plan-tools; task 7.5).

Publishes one named, validated plan version through platform ``POST /v1/plans/{plan_id}/publications``
with the publication ``expected_revision`` and the derived idempotency key, and returns the
platform-minted ``publication_id`` with the version checksum. A repeat with the same key and body
returns the original publication. The platform's ``PRECONDITION_FAILED`` for a version that is not
``validated``, and ``CONFLICT`` for a stale revision, pass through unchanged.

Publication is not execution: the request schema accepts no ``execute`` or ``mode`` field
(``VALIDATION_FAILED`` before any call), and no tool creates executions, places orders or calls a
brokerage, exchange, payment or wallet system. Phase 1: synthetic portfolios only.
"""

from __future__ import annotations

from typing import Any

from ..core.errors import ToolError
from ..core.registry import register_tool
from ._common import producer_doc, project
from ._plan_guard import require_synthetic_plan

RESPONSE = "tools/publish-plan-version-response"


@register_tool(
    "publish_plan_version",
    description=(
        "Publish one validated plan version of a plan (pass the plan's current revision as "
        "expected_revision). Returns the platform-minted publication_id and the published version's "
        "checksum. Publication records the chosen version only; it never executes trades. Requires an "
        "idempotency_key."
    ),
)
def publish_plan_version(ctx: Any, request: dict[str, Any]) -> dict[str, Any]:
    require_synthetic_plan(ctx, request["plan_id"])
    result = producer_doc(ctx.platform.publish_plan_version(request["plan_id"], ctx.downstream_body(request, drop=("plan_id",)), ctx.meta), "publication")
    if result.get("plan_version_id") != request["plan_version_id"] or result.get("plan_id") != request["plan_id"]:
        raise ToolError.internal("the platform published another version than requested")
    doc = project(result, RESPONSE)
    if request.get("synthetic") is True:
        doc["synthetic"] = True
    return doc
