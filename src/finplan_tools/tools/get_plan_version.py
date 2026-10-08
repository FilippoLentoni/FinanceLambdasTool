"""``get_plan_version`` (spec plan-tools; task 7.1).

Platform ``GET /v1/plan-versions/{plan_version_id}``: identifiers, lineage
(``parent_plan_version_id``, ``input_snapshot_id``, ``configuration_id``, ``model_version``,
``run_id``), origin, status, checksum, the platform's compact content summary (top-N plus other) and
the trusted content reference. The tool returns what the website path reads (same id, checksum and
canonical content reference) and never asks for a download grant. Unknown version -> ``NOT_FOUND``.
"""

from __future__ import annotations

from typing import Any

from ..core.bounds import response_size
from ..core.errors import ToolError
from ..core.registry import register_tool
from ._common import producer_doc, project

RESPONSE = "tools/get-plan-version-response"


@register_tool(
    "get_plan_version",
    description=(
        "Read one immutable plan version by plan_version_id: plan_id, parent version, origin, status "
        "(pending_validation, validated, invalid), lineage (snapshot, configuration, model version, run), "
        "checksum, a compact allocation summary and a trusted reference to the full content. Read-only."
    ),
)
def get_plan_version(ctx: Any, request: dict[str, Any]) -> dict[str, Any]:
    body = producer_doc(ctx.platform.get_plan_version(request["plan_version_id"], ctx.meta), "plan version")
    version = producer_doc(body.get("plan_version"), "plan version")
    if version.get("plan_version_id") != request["plan_version_id"]:
        raise ToolError.internal("the platform returned another plan version than requested")
    doc = project(body, RESPONSE)
    doc.setdefault("content_ref", version.get("content_ref"))
    if doc.get("content_ref") is None:
        doc.pop("content_ref")
    if "content_summary" in doc and response_size(doc) > ctx.limits.response_max_bytes:
        doc.pop("content_summary")
        doc["truncated"] = True
    doc.setdefault("truncated", False)
    if request.get("synthetic") is True:
        doc["synthetic"] = True
    return doc
