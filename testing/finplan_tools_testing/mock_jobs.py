"""In-process FinanceModel job API double (TEST-ONLY).

Routes as ``finplan_model.control.api``: ``POST v1/jobs`` (``submit_job``, with ``dry_run``),
``GET v1/jobs/{run_id}``, ``GET v1/jobs/{run_id}/result``; ``approve`` and ``cancel`` exist and
answer ``FORBIDDEN`` (tool roles may never call them) so a test can assert they were never called.

Modelled: content-addressed ``configuration_id``; cost estimate per job type and budget category
(:attr:`MockJobApi.estimates`); FinanceModel's own category pre-flight against
:attr:`remaining_by_category` (``BUDGET_EXCEEDED``); ``production_candidate`` refused; GPU or
over-auto-approve runs held in ``awaiting_approval``; idempotent submission (replay or
``IDEMPOTENCY_KEY_REUSED``); outcome control with :meth:`finish` (optimal, infeasible, no_effect,
failed, timed-out partial ...); the 1.1.0 production-strategy selection
(``GET``/``PUT v1/production-strategy``: registry validation with ``no_evaluation_evidence``,
confirmation, idempotency). Every answer validates against its contract schema.
"""

from __future__ import annotations

import copy
from typing import Any

from finplan_contracts.canonical import configuration_id

from finplan_tools.core.contracts import contract_version, validate_document

from ._base import MockProducer, fixture, require_valid

FIXTURE_JOB_TYPES = ("fixture_optimizer", "fixture_stub")


class MockJobApi(MockProducer):
    producer = "financemodel"

    def __init__(self, **kw: Any) -> None:
        super().__init__(**kw)
        #: job_type -> (estimated USD upper bound, budget category, compute class)
        self.estimates: dict[str, tuple[float, str, str]] = {
            "fixture_optimizer": (0.0, "cpu_research", "cpu"),
            "fixture_stub": (0.0, "cpu_research", "cpu"),
            "cpu_backtest": (1.40, "cpu_research", "cpu"),
            "cpu_small": (0.35, "cpu_research", "cpu"),
            "gpu_training": (3.00, "gpu", "gpu"),
            "explain_batch": (0.10, "bedrock_explanations", "cpu"),
        }
        self.remaining_by_category: dict[str, float] = {"platform_infra": 8.0, "cpu_research": 7.0, "bedrock_explanations": 5.0, "gpu": 25.0, "reserve": 5.0}
        self.auto_approve_usd = 0.0
        self.runs: dict[str, dict[str, Any]] = {}
        self.environment = "beta"
        #: FinanceModel strategy registry: registered strategies and those with evaluation evidence.
        self.registered_strategies: set[str] = {"buy_and_hold", "momentum_12_1"}
        self.evaluated_strategies: set[str] = {"buy_and_hold"}
        self.production_strategy: dict[str, Any] | None = None
        r = self.route
        r("POST", "v1/jobs", "submit_job", self._submit)
        r("GET", "v1/jobs/{run_id}", "get_job_status", self._status)
        r("GET", "v1/jobs/{run_id}/result", "get_job_result", self._result)
        r("POST", "v1/jobs/{run_id}/approve", "approve_run", lambda request, run_id: self.error("FORBIDDEN", "tool roles may not approve runs"))
        r("POST", "v1/jobs/{run_id}/cancel", "cancel_job", lambda request, run_id: self.error("FORBIDDEN", "tool roles may not cancel runs"))
        r("GET", "v1/production-strategy", "get_production_strategy", self._get_strategy)
        r("PUT", "v1/production-strategy", "put_production_strategy", self._put_strategy)

    # ================================================================ production strategy (1.1.0)
    def _get_strategy(self, request: Any) -> tuple[int, Any]:
        return 200, {"strategy": copy.deepcopy(self.production_strategy)}

    def _put_strategy(self, request: Any) -> tuple[int, Any]:
        body = request.body or {}
        action = body.get("action")
        if action not in ("set", "clear"):
            return self.error("VALIDATION_FAILED", "action must be set or clear", pointer="/action")
        if body.get("confirmed_by_user") is not True:
            return self.error("PRECONDITION_FAILED", "user confirmation is required", reason="confirmation_required")

        def apply() -> tuple[int, Any]:
            before = copy.deepcopy(self.production_strategy)
            if action == "clear":
                self.production_strategy = None
            else:
                sid = body.get("strategy_id")
                if sid not in self.registered_strategies:
                    return self.error("VALIDATION_FAILED", "the strategy is not registered", reason="unknown_strategy", pointer="/strategy_id")
                if sid not in self.evaluated_strategies:
                    return self.error("VALIDATION_FAILED", "the strategy has no evaluation evidence", reason="no_evaluation_evidence", pointer="/strategy_id", strategy_id=sid)
                doc = {"strategy_id": sid, "environment": self.environment, "selected_at": self.clock.iso(), "selected_by": "synthetic-operator", "contract_version": contract_version()}
                if body.get("synthetic") is True:
                    doc["synthetic"] = True
                self.production_strategy = require_valid(doc, "production-strategy")
            return 200, {"strategy": copy.deepcopy(self.production_strategy), "changed": before != self.production_strategy}

        return self.idempotent("put_production_strategy", body, apply)

    # ================================================================ submit
    def _estimate(self, job_type: str) -> dict[str, Any]:
        usd, cat, _ = self.estimates[job_type]
        return {"estimated_usd_upper_bound": usd, "price_retrieved_at": self.clock.iso(), "remaining_allocation_usd": self.remaining_by_category.get(cat, 0.0), "budget_category": cat, "synthetic": True}

    def _submit(self, request: Any) -> tuple[int, Any]:
        body = request.body or {}
        # FinanceModel validates the contract job-submission envelope (dry_run and contract_version required)
        res = validate_document(body, "job-submission")
        if not res.valid:
            env = res.to_error_envelope("mock-producer-0001", contract_version())
            return 400, env
        if body.get("job_type") not in self.estimates:
            return self.error("VALIDATION_FAILED", "job type is not available in this release", pointer="/job_type")
        if body.get("purpose") == "production_candidate":
            return self.error("FORBIDDEN", "production_candidate runs are not allowed for this principal")
        cfg_id = configuration_id(body["configuration"])
        est = self._estimate(body["job_type"])
        if est["estimated_usd_upper_bound"] > self.remaining_by_category.get(est["budget_category"], 0.0) + 1e-12:
            return self.error("BUDGET_EXCEEDED", "the estimate exceeds the remaining category allocation", budget_category=est["budget_category"], estimated_usd_upper_bound=est["estimated_usd_upper_bound"])
        if body.get("dry_run") is True:
            resp = {"run_id": None, "configuration_id": cfg_id, "state": None, "dry_run": True, "cost_estimate": est, "message": "Dry run: no run was recorded.", "synthetic": True}
            return 200, require_valid(resp, "tools/submit-experiment-response")

        def run() -> tuple[int, Any]:
            _, cat, compute = self.estimates[body["job_type"]]
            needs_approval = compute == "gpu" or cat == "gpu" or est["estimated_usd_upper_bound"] > self.auto_approve_usd + 1e-12
            state = "awaiting_approval" if needs_approval else "queued"
            run_id = self.ids.mint("run")
            now = self.clock.iso()
            self.runs[run_id] = {
                "run_id": run_id,
                "state": state,
                "purpose": body["purpose"],
                "compute_class": compute,
                "cost_estimate": est,
                "configuration_id": cfg_id,
                "input_snapshot_id": body["input_snapshot_id"],
                "domain": body["domain"],
                "domain_schema_version": body["domain_schema_version"],
                "job_type": body["job_type"],
                "submitted_at": now,
                "updated_at": now,
                "transitions": [{"state": state, "at": now}],
                "result": None,
            }
            resp = {"run_id": run_id, "configuration_id": cfg_id, "state": state, "dry_run": False, "cost_estimate": est, "message": "Awaiting human approval." if needs_approval else "Queued.", "synthetic": True}
            return 202, require_valid(resp, "tools/submit-experiment-response")

        return self.idempotent("submit_job", body, run)

    # ================================================================ outcome control
    def set_state(self, run_id: str, state: str) -> None:
        run = self.runs[run_id]
        run["state"] = state
        run["updated_at"] = self.clock.iso()
        run["transitions"].append({"state": state, "at": run["updated_at"]})

    def finish(self, run_id: str, completion: str = "succeeded", solution: str | None = "optimal", *, artifacts_complete: bool | None = None, payload_extra: dict[str, Any] | None = None) -> None:
        """Move a run to a terminal state with a contract job-result built from package fixtures."""
        template = {
            ("succeeded", "optimal"): "succeeded-optimal",
            ("succeeded", "infeasible"): "succeeded-infeasible",
            ("succeeded", "no_effect"): "succeeded-no-effect",
            ("succeeded", "unbounded"): "succeeded-unbounded",
            ("failed", None): "failed-crash",
            ("cancelled", None): "cancelled",
            ("timed_out", None): "timed-out-partial-artifacts",
        }[(completion, solution if completion == "succeeded" else None)]
        run = self.runs[run_id]
        result = fixture("job-result", template)
        result.update(run_id=run_id, configuration_id=run["configuration_id"], input_snapshot_id=run["input_snapshot_id"], completed_at=self.clock.iso())
        if artifacts_complete is not None:
            result["artifacts_complete"] = artifacts_complete
        if payload_extra and isinstance(result.get("payload"), dict):
            result["payload"].update(copy.deepcopy(payload_extra))
        run["result"] = require_valid(result, "job-result")
        if completion == "succeeded" and solution:
            run["solution_status"] = solution
        self.set_state(run_id, completion)

    # ================================================================ reads
    def _status_doc(self, run: dict[str, Any]) -> dict[str, Any]:
        doc = {k: copy.deepcopy(run[k]) for k in ("run_id", "state", "purpose", "compute_class", "cost_estimate", "transitions", "configuration_id", "input_snapshot_id", "domain", "submitted_at", "updated_at")}
        doc.update(dry_run=False, elapsed_seconds=0, job_type=run["job_type"], synthetic=True)
        if run["state"] in ("succeeded", "failed", "cancelled", "timed_out"):
            doc["completion_status"] = run["state"]
        if run["state"] == "succeeded" and run.get("solution_status"):
            doc["solution_status"] = run["solution_status"]
        if run["state"] == "failed" and run.get("result") and run["result"].get("error"):
            doc["error"] = copy.deepcopy(run["result"]["error"])
        if run["state"] == "awaiting_approval":
            doc["wait_reason"] = "awaiting_human_approval"
        return doc

    def _status(self, request: Any, run_id: str) -> tuple[int, Any]:
        run = self.runs.get(run_id)
        if run is None:
            return self.error("NOT_FOUND", "run not found", record_type="run")
        return 200, require_valid(self._status_doc(run), "job-status")

    def _result(self, request: Any, run_id: str) -> tuple[int, Any]:
        run = self.runs.get(run_id)
        if run is None:
            return self.error("NOT_FOUND", "run not found", record_type="run")
        if run["result"] is None:
            return self.error("PRECONDITION_FAILED", "the run has not finished; no result yet", reason="run_not_terminal", state=run["state"])
        return 200, require_valid(copy.deepcopy(run["result"]), "job-result")


def research_request(**overrides: Any) -> dict[str, Any]:
    """The contract's valid ``submit_experiment`` research request (synthetic), with overrides."""
    req = fixture("tools/submit-experiment-request", "research")
    req.update(overrides)
    return req
