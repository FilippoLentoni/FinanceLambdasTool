"""``list_plan_versions`` (spec plan-tools; task 7.2).

Platform ``GET /v1/plans/{plan_id}/versions`` (G-1), newest first. Each entry: ``plan_version_id``,
parent, origin, status, checksum, creation time. Pagination: the tool's opaque ``next_token`` wraps
the platform's token of the page it read plus an offset into that page, bound to this tool and
environment. When the byte limit cuts a page, the next call re-reads the same platform page and
resumes at the first item not returned, so no entry is duplicated or skipped.
"""

from __future__ import annotations

from typing import Any

from ..core.errors import ToolError
from ..core.registry import register_tool
from ._common import producer_doc

ENTRY_KEYS = ("plan_version_id", "parent_plan_version_id", "origin", "status", "checksum", "created_at")


@register_tool(
    "list_plan_versions",
    description=(
        "List the versions of a plan newest first (plan_version_id, parent, origin, status, checksum, "
        "creation time), one page at a time; pass the returned next_token to get the following page. Read-only."
    ),
)
def list_plan_versions(ctx: Any, request: dict[str, Any]) -> dict[str, Any]:
    limits = ctx.limits
    size = min(int(request.get("page_size") or limits.page_size_default), limits.page_size_max)
    producer_token, offset = None, 0
    if request.get("next_token") is not None:
        state = ctx.unwrap_token(request["next_token"])
        producer_token, offset = state.get("producer_token"), int(state.get("offset") or 0)
    page = producer_doc(ctx.platform.list_plan_versions(request["plan_id"], ctx.meta, page_size=size, next_token=producer_token), "version list")
    if page.get("plan_id", request["plan_id"]) != request["plan_id"]:
        raise ToolError.internal("the platform listed another plan than requested")
    entries = [{k: v.get(k) for k in ENTRY_KEYS} for v in page.get("versions") or () if isinstance(v, dict)]
    rest = entries[offset:]
    nxt = page.get("next_token")
    doc: dict[str, Any] = {
        "plan_id": request["plan_id"],
        "versions": rest,
        # the platform's next page starts after the page just read
        "next_token": ctx.wrap_token(producer_token=nxt) if isinstance(nxt, str) and nxt else None,
    }
    if request.get("synthetic") is True or page.get("synthetic") is True:
        doc["synthetic"] = True
    bounded = ctx.bound(doc, "versions", start_offset=offset, producer_token=producer_token, truncated_key=None)
    return bounded
