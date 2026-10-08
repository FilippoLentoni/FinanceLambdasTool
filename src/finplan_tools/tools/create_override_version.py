"""``create_override_version`` (spec plan-tools; task 7.3).

Creates a *child* of a named parent through platform ``POST /v1/plans/{plan_id}/versions`` with the
caller's ``expected_revision`` and the derived idempotency key; the platform mints the
``plan_version_id`` and the parent never changes. Outcomes passed through unchanged: ``CONFLICT``
(stale revision, ``retryable`` false, re-read the plan head), ``no_effect`` true with the parent's
checksum, ``IDEMPOTENCY_KEY_REUSED``, ``OPERATION_NOT_PERMITTED``.

Versions are immutable: a request naming a ``plan_version_id`` to change in place is refused with
``IMMUTABLE_RECORD`` before any other check, directing the caller to create a child instead.
Phase 1: only plans of synthetic portfolios (:mod:`._plan_guard`).
"""

from __future__ import annotations

from typing import Any

from ..core.errors import CONFLICT, IMMUTABLE_RECORD, ToolError
from ..core.registry import register_tool
from ._common import producer_doc, project
from ._plan_guard import require_synthetic_plan

RESPONSE = "tools/create-override-version-response"
_IN_PLACE_FIELDS = ("plan_version_id", "target_plan_version_id")


def refuse_in_place_edit(request: dict[str, Any]) -> None:
    for field in _IN_PLACE_FIELDS:
        if field in request:
            raise ToolError(
                IMMUTABLE_RECORD,
                "plan versions are immutable; create a child version with parent_plan_version_id instead",
                pointer=f"/{field}",
                hint="create_child_version",
            )


@register_tool(
    "create_override_version",
    description=(
        "Create a new child plan version (origin manual_override) of a named parent version with changed "
        "content, passing the plan's current revision as expected_revision. The parent is never modified; "
        "existing versions cannot be edited. Returns the platform-minted plan_version_id, status, checksum "
        "and no_effect. Synthetic portfolios only in phase 1. Requires an idempotency_key."
    ),
    pre_validate=refuse_in_place_edit,
)
def create_override_version(ctx: Any, request: dict[str, Any]) -> dict[str, Any]:
    require_synthetic_plan(ctx, request["plan_id"])
    body = ctx.downstream_body(request)
    try:
        result = producer_doc(ctx.platform.create_plan_version(request["plan_id"], body, ctx.meta), "created version")
    except ToolError as err:
        if err.code == CONFLICT:
            details = dict(err.details)
            details.setdefault("hint", "re-read the plan head with get_plan and retry with its revision")
            raise ToolError(CONFLICT, err.message, retryable=False, details=details) from None
        raise
    if result.get("parent_plan_version_id") != request["parent_plan_version_id"] or result.get("origin") != "manual_override":
        raise ToolError.internal("the platform did not create a child of the named parent")
    doc = project(result, RESPONSE)
    if request.get("synthetic") is True:
        doc["synthetic"] = True
    return doc
