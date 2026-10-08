"""``get_job_status`` (spec experiment-tools; task 6.4).

FinanceModel ``GET /v1/jobs/{run_id}``: state, transition timestamps, elapsed runtime, cost estimate
and, for terminal runs only, ``completion_status`` (and ``solution_status`` when it succeeded). Only
the contract ``job-status`` fields are returned, so no storage location, job name or principal
reaches the caller. ``awaiting_approval`` is reported as a normal state with a note that a human
approver must act: no tool can approve or cancel a run (the job client has no such method).
"""

from __future__ import annotations

from typing import Any

from ..core.errors import ToolError
from ..core.registry import register_tool
from ..core.status import outcome
from ._common import producer_doc, project

RESPONSE = "tools/get-job-status-response"
APPROVAL_MESSAGE = "This run waits for a human approver in FinanceModel; no tool can approve it."


@register_tool(
    "get_job_status",
    description=(
        "Read the state of a FinanceModel experiment run by run_id: state, transition timestamps, "
        "elapsed runtime, cost estimate and, once terminal, completion_status. A run in "
        "awaiting_approval needs a human approver; tools cannot approve. Read-only."
    ),
)
def get_job_status(ctx: Any, request: dict[str, Any]) -> dict[str, Any]:
    status = producer_doc(ctx.jobs.get_job_status(request["run_id"], ctx.meta), "job status")
    if status.get("run_id") != request["run_id"]:
        raise ToolError.internal("FinanceModel returned the status of another run")
    doc = project(status, RESPONSE)
    view = outcome(status)
    if view["completion_status"] == "succeeded" and status.get("solution_status"):
        doc["solution_status"] = status["solution_status"]
    if doc.get("state") == "awaiting_approval":
        doc["message"] = APPROVAL_MESSAGE
    if request.get("synthetic") is True:
        doc["synthetic"] = True
    return doc
