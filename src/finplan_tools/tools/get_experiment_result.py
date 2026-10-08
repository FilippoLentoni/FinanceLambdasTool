"""``get_experiment_result`` (spec experiment-tools; task 6.5).

FinanceModel ``GET /v1/jobs/{run_id}/result`` (contract ``job-result``):

* ``completion_status`` and ``solution_status`` stay separate: infeasible, unbounded or no-effect
  outcomes are ``succeeded`` runs and a successful tool call; a crash is ``failed`` with
  FinanceModel's error envelope in ``error`` and no ``solution_status``;
* a non-terminal run -> FinanceModel's ``PRECONDITION_FAILED`` with ``details.state`` passed through;
* ``timed_out`` / ``cancelled`` runs or incomplete outputs: ``artifacts_complete`` false, only the
  references FinanceModel lists, and no summary metrics (the payload's metric sections would describe
  missing periods), with a note that the result is not a usable plan input;
* a succeeded result keeps FinanceModel's separate sections (portfolio ``performance``, model
  ``accuracy``, ``compute_cost``), the lineage identifiers and the artifact references with checksums;
  when it would exceed the byte limit the candidate allocation is left out (``truncated``) and stays
  reachable through the artifact references.

Read-only, FinanceModel only: no platform call, and a result never becomes a plan version through a
tool (promotion stays the platform's staged-output acceptance path).
"""

from __future__ import annotations

from typing import Any

from ..core.bounds import response_size
from ..core.errors import ToolError
from ..core.registry import register_tool
from ..core.status import outcome
from ._common import producer_doc, project

RESPONSE = "tools/get-experiment-result-response"
PARTIAL_MESSAGE = "The run did not complete with all outputs; only the listed references exist and the result is not a usable plan input."


@register_tool(
    "get_experiment_result",
    description=(
        "Read the result of a finished FinanceModel run by run_id: completion_status (did the run "
        "finish) separately from solution_status (optimal, feasible, infeasible, unbounded, no_effect), "
        "separate portfolio-performance, model-accuracy and compute-cost sections, lineage identifiers "
        "and trusted artifact references. Partial runs are flagged. Read-only."
    ),
)
def get_experiment_result(ctx: Any, request: dict[str, Any]) -> dict[str, Any]:
    result = producer_doc(ctx.jobs.get_job_result(request["run_id"], ctx.meta), "job result")
    if result.get("run_id") != request["run_id"]:
        raise ToolError.internal("FinanceModel returned the result of another run")
    doc = project(result, RESPONSE)
    view = outcome(result)
    if view["completion_status"] != "succeeded":
        doc.pop("solution_status", None)
    if view["partial"]:
        doc["artifacts_complete"] = False
        doc.pop("payload", None)
        doc["message"] = PARTIAL_MESSAGE
    elif isinstance(doc.get("payload"), dict) and response_size(doc) > ctx.limits.response_max_bytes:
        payload = dict(doc["payload"])
        if payload.pop("proposed_allocation", None) is not None:
            doc["payload"] = payload
            doc["truncated"] = True
            doc["message"] = "The candidate allocation is omitted to fit the response size; read it through the artifact references."
    if request.get("synthetic") is True:
        doc["synthetic"] = True
    return doc
