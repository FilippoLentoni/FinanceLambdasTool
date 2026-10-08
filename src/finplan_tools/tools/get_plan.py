"""``get_plan`` (spec plan-tools; task 7.2).

Platform ``GET /v1/plans/{plan_id}`` (G-1): the plan with its head (``current_version_id``,
``revision``) and its current publication, if any. The plan head is never cached (it is what
``expected_revision`` is checked against). A platform release without the route answers
``DEPENDENCY_UNAVAILABLE`` (route not deployed); an unknown plan ``NOT_FOUND``.
"""

from __future__ import annotations

from typing import Any

from ..core.errors import ToolError
from ..core.registry import register_tool
from ._common import producer_doc, project

RESPONSE = "tools/get-plan-response"


@register_tool(
    "get_plan",
    description=(
        "Read a plan by plan_id: its current head version id and revision (use the revision as "
        "expected_revision for overrides and publication) and its current publication, if any. Read-only."
    ),
)
def get_plan(ctx: Any, request: dict[str, Any]) -> dict[str, Any]:
    body = producer_doc(ctx.platform.get_plan(request["plan_id"], ctx.meta), "plan")
    plan = producer_doc(body.get("plan"), "plan")
    if plan.get("plan_id") != request["plan_id"]:
        raise ToolError.internal("the platform returned another plan than requested")
    doc = project(body, RESPONSE)
    doc.setdefault("current_publication", None)
    if request.get("synthetic") is True:
        doc["synthetic"] = True
    return doc
