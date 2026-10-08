"""``submit_experiment`` (spec experiment-tools; tasks 6.1, 6.2, 6.3; design D5).

Flow (every rejection before step 5 submits nothing):

1. the request already passed the contract schema, including the finance configuration payload
   (pipeline); the configuration must name the request's domain and schema version;
2. purpose ``production_candidate`` -> ``FORBIDDEN`` with no producer call;
3. ``configuration_id`` computed locally (contract canonicalization);
4. the snapshot is read through the platform in this environment (unknown -> ``NOT_FOUND``) and must
   have the request's domain and a coverage spanning the evaluation window (``PRECONDITION_FAILED``
   with the coverage in ``details``);
5. FinanceModel ``submit_job`` with ``dry_run`` true -> estimate and ``budget_category``; a
   ``configuration_id`` differing from the local one -> ``INTERNAL`` (both logged);
6. a caller dry run returns here (``run_id`` null, estimate, whether it is within the tool limit);
7. per-call tool limit by budget category (``tool-limits.max_estimated_usd_per_call``): over the
   limit or no limit -> ``BUDGET_EXCEEDED`` with estimate, category and limit, nothing submitted;
8. ``submit_job`` with the derived key; FinanceModel mints ``run_id`` and returns ``queued`` or
   ``awaiting_approval`` immediately (the tool never waits, never approves, never mints).

FinanceModel's own ``BUDGET_EXCEEDED``, ``VALIDATION_FAILED`` / ``DEPENDENCY_UNAVAILABLE`` for
job types and ``IDEMPOTENCY_KEY_REUSED`` pass through unchanged. The downstream body is the
contract ``core/v1/job-submission.json`` document FinanceModel validates: the tool request without
``dry_run``, plus ``dry_run`` and the pinned ``contract_version``, with the derived key.
"""

from __future__ import annotations

import logging
from typing import Any

from ..core.budget import check_tool_budget
from ..core.compat import check_configuration_id, check_snapshot_compatibility, local_configuration_id
from ..core.contracts import contract_version
from ..core.errors import FORBIDDEN, ToolError
from ..core.registry import register_tool
from ._common import producer_doc, project

log = logging.getLogger("finplan_tools.tools.submit_experiment")
RESPONSE = "tools/submit-experiment-response"
ALLOWED_PURPOSES = ("research", "tuning", "holdout_evaluation")
APPROVAL_MESSAGE = "A human approver must approve this run in FinanceModel before it starts; no tool can approve it."


def submission_body(ctx: Any, request: dict[str, Any], *, dry_run: bool) -> dict[str, Any]:
    """The FinanceModel ``submit_job`` body (contract ``job-submission``), deterministic per request."""
    return ctx.downstream_body(request, drop=("dry_run",), extra={"dry_run": dry_run, "contract_version": contract_version()})


def _check_cfg(local: str, resp: dict[str, Any], correlation_id: str) -> None:
    if resp.get("configuration_id") != local:
        log.error("configuration_id mismatch (correlation_id=%s local=%s producer=%s)", correlation_id, local, resp.get("configuration_id"))
    check_configuration_id(local, resp.get("configuration_id"))


@register_tool(
    "submit_experiment",
    description=(
        "Submit an asynchronous FinanceModel experiment (backtest, optimization, or job_type "
        "model_selection: controls vs traditional optimizers vs RL with the configuration's frozen "
        "train/validation/test protocol; configuration.payload.strategy model_selection, purpose "
        "research, evaluation_window inside the snapshot coverage) on an existing "
        "snapshot of this environment. Validates the configuration, the snapshot coverage and the "
        "per-call cost limit for the run's budget category first; returns the FinanceModel run_id and "
        "state (queued or awaiting_approval) immediately. dry_run returns the estimate only. Purposes: "
        "research, tuning, holdout_evaluation. Requires an idempotency_key."
    ),
)
def submit_experiment(ctx: Any, request: dict[str, Any]) -> dict[str, Any]:
    cfg = request["configuration"]
    if cfg.get("domain") != request["domain"] or cfg.get("domain_schema_version") != request["domain_schema_version"]:
        raise ToolError.validation("the configuration must name the request's domain and domain_schema_version", pointer="/configuration/domain")
    if request["purpose"] not in ALLOWED_PURPOSES:
        raise ToolError(FORBIDDEN, f"purpose {request['purpose']} cannot be submitted through tools", reason="purpose_not_allowed", allowed_purposes=list(ALLOWED_PURPOSES))
    local_cfg = local_configuration_id(cfg)
    snap = producer_doc(producer_doc(ctx.platform.get_snapshot(request["input_snapshot_id"], ctx.meta), "snapshot").get("snapshot"), "snapshot")
    if snap.get("input_snapshot_id") != request["input_snapshot_id"]:
        raise ToolError.internal("the platform returned another snapshot than requested")
    check_snapshot_compatibility(snap, domain=request["domain"], window=request["evaluation_window"])

    estimate = producer_doc(ctx.jobs.submit_job(submission_body(ctx, request, dry_run=True), ctx.meta), "dry-run result")
    _check_cfg(local_cfg, estimate, ctx.correlation_id)
    cost = producer_doc(estimate.get("cost_estimate"), "cost estimate")
    if request.get("dry_run") is True:
        doc = project(estimate, RESPONSE)
        doc.update(run_id=None, state=None, dry_run=True, configuration_id=local_cfg)
        try:
            check_tool_budget(cost, ctx.limits)
            within, limit = True, ctx.limits.per_call_limit(cost["budget_category"])
        except ToolError as err:
            if err.code != "BUDGET_EXCEEDED":
                raise
            within, limit = False, err.details.get("limit_usd")
        doc["tool_limit"] = {"budget_category": cost.get("budget_category"), "limit_usd": limit, "within_limit": within}
        doc["message"] = "Dry run: validated and estimated; no run was recorded." + ("" if within else " A real submission would be rejected by the per-call tool limit.")
        if request.get("synthetic") is True:
            doc["synthetic"] = True
        return doc

    check_tool_budget(cost, ctx.limits)
    result = producer_doc(ctx.jobs.submit_job(submission_body(ctx, request, dry_run=False), ctx.meta), "submission result")
    _check_cfg(local_cfg, result, ctx.correlation_id)
    if not result.get("run_id"):
        raise ToolError.internal("FinanceModel accepted the submission without a run_id")
    doc = project(result, RESPONSE)
    doc["dry_run"] = False
    if doc.get("state") == "awaiting_approval":
        doc["message"] = APPROVAL_MESSAGE
    if request.get("synthetic") is True:
        doc["synthetic"] = True
    ctx.record_ids(doc)
    return doc
