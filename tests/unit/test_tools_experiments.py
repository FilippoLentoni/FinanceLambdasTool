"""Experiment tools (tasks 6.1-6.5): submit_experiment, get_job_status, get_experiment_result.

Run through the real handler and clients against the mock platform and mock FinanceModel job API.
"""

from __future__ import annotations

import ast
import json
import re
from pathlib import Path

import pytest

from finplan_tools.backends.jobs import JobClient
from finplan_tools.backends.platform import PlatformClient
from finplan_tools.core.artifacts import response_leaks
from finplan_tools.core.compat import local_configuration_id
from finplan_tools.core.config import ToolLimits
from finplan_tools.core.contracts import contract_version, validate_document
from finplan_tools.core.idempotency import DERIVED_KEY_PATTERN
from finplan_tools_testing.mock_jobs import research_request
from finplan_tools_testing.scenarios import SCENARIOS

SRC = Path(__file__).resolve().parents[2] / "src" / "finplan_tools"


def is_error(resp, code=None):
    return {"code", "message", "retryable", "correlation_id"} <= set(resp) and (code is None or resp["code"] == code)


@pytest.fixture
def snap(offline):
    return offline.platform.add_snapshot()


def req(snap, **kw):
    return research_request(input_snapshot_id=snap, **kw)


# =================================================================== submit_experiment
def test_exp01_valid_fixture_submission_returns_immediately(offline, invoke, snap):
    resp = invoke(offline, "submit_experiment", req(snap))
    assert not is_error(resp), resp
    assert resp["run_id"] in offline.jobs.runs and resp["state"] == "queued" and resp["dry_run"] is False
    assert resp["configuration_id"] == local_configuration_id(req(snap)["configuration"])
    assert offline.platform.ops() == ["get_snapshot"]
    assert offline.jobs.ops() == ["submit_job", "submit_job"]
    dry, real = (c.body for c in offline.jobs.calls)
    assert dry["dry_run"] is True and real["dry_run"] is False
    for body in (dry, real):  # what FinanceModel validates (contract job-submission)
        assert validate_document(body, "job-submission").valid
        assert body["contract_version"] == contract_version() and DERIVED_KEY_PATTERN.match(body["idempotency_key"])
        assert "configuration_id" not in body and "run_id" not in body


def test_exp02_model_absent_dependency_unavailable(offline, invoke, snap):
    offline.remove_model()
    resp = invoke(offline, "submit_experiment", req(snap))
    assert is_error(resp, "DEPENDENCY_UNAVAILABLE") and resp["retryable"] is False
    assert offline.jobs.count() == 0 and offline.platform.count() == 0


@pytest.mark.parametrize("tool,args", [("get_job_status", {"run_id": "run_01KM0000000000000000000001"}), ("get_experiment_result", {"run_id": "run_01KM0000000000000000000001"})])
def test_exp02_read_tools_gated_on_model_release(offline, invoke, tool, args):
    offline.remove_model()
    assert is_error(invoke(offline, tool, args), "DEPENDENCY_UNAVAILABLE") and offline.jobs.count() == 0


def test_exp03_window_outside_coverage(offline, invoke, snap):
    resp = invoke(offline, "submit_experiment", req(snap, evaluation_window={"start": "2026-01-02", "end": "2026-01-30"}))
    assert is_error(resp, "PRECONDITION_FAILED")
    assert resp["details"]["coverage"] == {"start": "2026-01-02", "end": "2026-01-09"}
    assert offline.jobs.count() == 0


def test_exp03_unknown_snapshot_not_found(offline, invoke):
    resp = invoke(offline, "submit_experiment", req("snap_01KM9999999999999999999999"))
    assert is_error(resp, "NOT_FOUND") and offline.jobs.count() == 0


def test_exp04_production_candidate_forbidden_without_calls(offline, invoke, snap):
    resp = invoke(offline, "submit_experiment", req(snap, purpose="production_candidate"))
    assert is_error(resp, "FORBIDDEN") and offline.jobs.count() == 0 and offline.platform.count() == 0


@pytest.mark.parametrize("purpose", ["research", "tuning", "holdout_evaluation"])
def test_exp04_allowed_purposes(offline, invoke, snap, purpose):
    assert not is_error(invoke(offline, "submit_experiment", req(snap, purpose=purpose)))


def test_exp08_configuration_id_mismatch_is_internal(offline, invoke, snap, monkeypatch, caplog):
    import finplan_tools.tools.submit_experiment as mod

    other = "cfg_" + "0" * 64
    monkeypatch.setattr(mod, "local_configuration_id", lambda cfg: other)
    resp = invoke(offline, "submit_experiment", req(snap))
    assert is_error(resp, "INTERNAL") and not offline.jobs.runs
    assert offline.jobs.count("submit_job") == 1  # the dry run only
    assert other in caplog.text and local_configuration_id(req(snap)["configuration"]) in caplog.text


def test_exp_invalid_strategy_parameter_points_at_field(offline, invoke, snap):
    r = req(snap)
    r["configuration"]["payload"]["risk_aversion"] = -5
    resp = invoke(offline, "submit_experiment", r)
    assert is_error(resp, "VALIDATION_FAILED") and "risk_aversion" in json.dumps(resp["details"])
    assert offline.jobs.count() == 0


def test_exp_configuration_domain_must_match(offline, invoke, snap):
    r = req(snap)
    r["configuration"]["domain_schema_version"] = "1.1"
    resp = invoke(offline, "submit_experiment", r)
    assert is_error(resp, "VALIDATION_FAILED") and offline.jobs.count() == 0


def test_exp_unsupported_job_type_passed_through(offline, invoke, snap):
    resp = invoke(offline, "submit_experiment", req(snap, job_type="rl_train"))
    assert is_error(resp, "VALIDATION_FAILED") and not offline.jobs.runs
    offline.jobs.fail_next("submit_job", "DEPENDENCY_UNAVAILABLE", retryable=False)
    resp = invoke(offline, "submit_experiment", req(snap, job_type="rl_train", idempotency_key="exp-key-0099"))
    assert is_error(resp, "DEPENDENCY_UNAVAILABLE") and resp["retryable"] is False and not offline.jobs.runs


# ---------------------------------------------------------------- per-call budget (6.2)
def test_exp05_cpu_over_limit(offline, invoke):
    s = SCENARIOS["budget_cpu_over"](offline)
    resp = invoke(offline, s.tool, s.requests[0])
    assert is_error(resp, "BUDGET_EXCEEDED") and resp["retryable"] is False
    d = resp["details"]
    assert d["estimated_usd_upper_bound"] == 1.40 and d["budget_category"] == "cpu_research" and d["limit_usd"] == 1.00
    assert len(offline.jobs.runs) == s.expect["runs"] and offline.jobs.count("submit_job") == 1


def test_exp05_zero_limit_category(offline, invoke):
    s = SCENARIOS["budget_zero_limit_category"](offline)
    resp = invoke(offline, s.tool, s.requests[0])
    assert is_error(resp, "BUDGET_EXCEEDED") and resp["details"]["limit_usd"] == 0.0 and not offline.jobs.runs


def test_exp05_category_missing_from_limits(offline_factory, invoke):
    o = offline_factory("beta", tool_limits={"max_estimated_usd_per_call": {"cpu_research": 1.0}})
    sid = o.platform.add_snapshot()
    resp = invoke(o, "submit_experiment", req(sid, job_type="gpu_training"))
    assert is_error(resp, "BUDGET_EXCEEDED") and resp["details"]["limit_usd"] is None and not o.jobs.runs


def test_exp05_gpu_within_limit_awaits_approval(offline, invoke):
    s = SCENARIOS["budget_gpu_within"](offline)
    resp = invoke(offline, s.tool, s.requests[0])
    assert resp["state"] == "awaiting_approval" and resp["cost_estimate"]["budget_category"] == "gpu"
    assert "approver" in resp["message"] and len(offline.jobs.runs) == 1


def test_exp05_fixture_estimate_zero_passes_any_limit(offline_factory, invoke):
    zero = {"max_estimated_usd_per_call": {c: 0 for c in ("platform_infra", "cpu_research", "bedrock_explanations", "gpu", "reserve")}}
    o = offline_factory("beta", tool_limits=zero)
    sid = o.platform.add_snapshot()
    assert invoke(o, "submit_experiment", req(sid))["state"] == "queued"


def test_exp05_financemodel_budget_rejection_passed_through(offline, invoke, snap):
    offline.jobs.remaining_by_category["cpu_research"] = 0.1
    resp = invoke(offline, "submit_experiment", req(snap, job_type="cpu_small"))
    assert is_error(resp, "BUDGET_EXCEEDED") and resp["retryable"] is False
    assert offline.jobs.count("submit_job") == 1 and not offline.jobs.runs


def test_exp06_dry_run_returns_estimate_only(offline, invoke, snap):
    resp = invoke(offline, "submit_experiment", req(snap, dry_run=True))
    assert resp["dry_run"] is True and resp["run_id"] is None and resp["configuration_id"].startswith("cfg_")
    assert resp["cost_estimate"]["estimated_usd_upper_bound"] == 0.0 and resp["tool_limit"]["within_limit"] is True
    assert not offline.jobs.runs and offline.jobs.count("submit_job") == 1


def test_exp06_dry_run_over_limit_reports_without_error(offline, invoke, snap):
    resp = invoke(offline, "submit_experiment", req(snap, dry_run=True, job_type="cpu_backtest"))
    assert resp["run_id"] is None and resp["tool_limit"] == {"budget_category": "cpu_research", "limit_usd": 1.0, "within_limit": False}
    assert not offline.jobs.runs


def test_trh06_duplicate_submission_one_run(offline, invoke):
    s = SCENARIOS["duplicate"](offline)
    a, b = (invoke(offline, s.tool, r) for r in s.requests)
    assert a["run_id"] == b["run_id"] and len(offline.jobs.runs) == 1


def test_key_reuse_with_other_body(offline, invoke):
    s = SCENARIOS["key_reuse"](offline)
    invoke(offline, s.tool, s.requests[0])
    resp = invoke(offline, s.tool, s.requests[1])
    assert is_error(resp, "IDEMPOTENCY_KEY_REUSED") and len(offline.jobs.runs) == 1


def test_prod_direct_submission_forbidden(offline_factory, invoke):
    o = offline_factory("prod")
    sid = o.platform.add_snapshot()
    assert is_error(invoke(o, "submit_experiment", req(sid)), "FORBIDDEN") and o.jobs.count() == 0


# =================================================================== get_job_status
def _submitted(o, invoke, job_type="fixture_optimizer"):
    sid = o.platform.add_snapshot()
    return invoke(o, "submit_experiment", req(sid, job_type=job_type))["run_id"]


def test_exp09_running_state(offline, invoke):
    rid = _submitted(offline, invoke)
    offline.jobs.set_state(rid, "running")
    resp = invoke(offline, "get_job_status", {"run_id": rid})
    assert resp["state"] == "running" and "completion_status" not in resp
    assert [t["state"] for t in resp["transitions"]] == ["queued", "running"]
    assert resp["submitted_at"] and resp["updated_at"] and response_leaks(resp) == []
    assert set(resp) <= {"run_id", "state", "completion_status", "solution_status", "purpose", "dry_run", "compute_class", "cost_estimate", "approval", "transitions", "elapsed_seconds", "configuration_id", "input_snapshot_id", "model_version", "domain", "error", "submitted_at", "updated_at", "synthetic", "message"}


def test_exp09_terminal_has_completion_status(offline, invoke):
    rid = _submitted(offline, invoke)
    offline.jobs.finish(rid, "succeeded", "infeasible")
    resp = invoke(offline, "get_job_status", {"run_id": rid})
    assert resp["completion_status"] == "succeeded" and resp["solution_status"] == "infeasible"


def test_exp07_awaiting_approval_reported_never_approved(offline, invoke):
    rid = _submitted(offline, invoke, "gpu_training")
    resp = invoke(offline, "get_job_status", {"run_id": rid})
    assert resp["state"] == "awaiting_approval" and "approver" in resp["message"]
    assert offline.jobs.count("approve_run") == 0 and offline.jobs.count("cancel_job") == 0


def test_exp07_no_approval_or_cancel_code_path():
    """Static check: no client method or route string can approve or cancel a run."""
    for cls in (JobClient, PlatformClient):
        names = [n for n in dir(cls) if not n.startswith("_")]
        assert not [n for n in names if re.search(r"approve|cancel|execut|order|trade", n)], cls
    route = re.compile(r"""["'][^"']*/(approve|cancel|executions)\b""")
    for p in SRC.rglob("*.py"):
        tree = ast.parse(p.read_text(encoding="utf-8"))
        strings = [n.value for n in ast.walk(tree) if isinstance(n, ast.Constant) and isinstance(n.value, str)]
        docstrings = {ast.get_docstring(n) for n in ast.walk(tree) if isinstance(n, (ast.Module, ast.FunctionDef, ast.ClassDef))}
        for s in strings:
            if s not in docstrings:
                assert not route.search(repr(s)), f"{p}: {s!r}"


def test_job_status_unknown_run_not_found(offline, invoke):
    assert is_error(invoke(offline, "get_job_status", {"run_id": "run_01KM9999999999999999999999"}), "NOT_FOUND")


# =================================================================== get_experiment_result
@pytest.mark.parametrize("name,completion,solution", [("infeasible", "succeeded", "infeasible"), ("no_effect", "succeeded", "no_effect"), ("failed", "failed", None)])
def test_exp10_completion_separate_from_solution(offline, invoke, name, completion, solution):
    s = SCENARIOS[name](offline)
    resp = invoke(offline, s.tool, s.requests[0])
    assert not is_error(resp), resp
    assert resp["completion_status"] == completion and resp.get("solution_status") == solution
    if completion == "failed":
        assert resp["error"]["code"] and "solution_status" not in resp and resp["artifacts_complete"] is False


def test_exp10_optimal(offline, invoke):
    rid = _submitted(offline, invoke)
    offline.jobs.finish(rid, "succeeded", "optimal")
    resp = invoke(offline, "get_experiment_result", {"run_id": rid})
    assert resp["completion_status"] == "succeeded" and resp["solution_status"] == "optimal" and resp["artifacts_complete"] is True


def test_exp11_queued_run_precondition_failed(offline, invoke):
    rid = _submitted(offline, invoke)
    resp = invoke(offline, "get_experiment_result", {"run_id": rid})
    assert is_error(resp, "PRECONDITION_FAILED") and resp["details"]["state"] == "queued"


def test_exp12_timed_out_partial(offline, invoke):
    s = SCENARIOS["timed_out_partial"](offline)
    resp = invoke(offline, s.tool, s.requests[0])
    assert resp["completion_status"] == "timed_out" and resp["artifacts_complete"] is False
    assert "payload" not in resp and "not a usable plan input" in resp["message"]
    assert resp["artifacts"] == offline.jobs.runs[s.ids["run_id"]]["result"]["artifacts"]


def test_exp13_separate_sections_bounded_no_storage(offline, invoke):
    rid = _submitted(offline, invoke)
    offline.jobs.finish(rid)
    resp = invoke(offline, "get_experiment_result", {"run_id": rid})
    p = resp["payload"]
    assert {"performance", "accuracy", "compute_cost"} <= set(p)
    for k in ("evaluator_version", "dataset_checksum", "model_version", "configuration_id", "input_snapshot_id"):
        assert resp[k]
    assert all(a["checksum"].startswith("sha256:") for a in resp["artifacts"])
    assert len(json.dumps(resp)) < ToolLimits.from_document(None).response_max_bytes and response_leaks(resp) == []


def test_exp13_large_allocation_omitted_to_fit():
    from types import SimpleNamespace

    from finplan_tools.tools.get_experiment_result import get_experiment_result
    from finplan_tools_testing._base import fixture

    result = fixture("job-result", "succeeded-optimal")
    result["payload"]["proposed_allocation"]["weights"] = [{"instrument_id": f"I{i:04d}", "weight": 0.0001} for i in range(400)]
    jobs = SimpleNamespace(get_job_result=lambda run_id, meta: result)
    ctx = SimpleNamespace(jobs=jobs, meta=None, limits=ToolLimits.from_document({"response_max_bytes": 4096}))
    doc = get_experiment_result(ctx, {"run_id": result["run_id"]})
    assert doc["truncated"] is True and "proposed_allocation" not in doc["payload"] and len(json.dumps(doc)) < 4096
    assert validate_document(doc, "tools/get-experiment-result-response").valid


def test_exp14_no_platform_call(offline, invoke):
    rid = _submitted(offline, invoke)
    offline.jobs.finish(rid)
    offline.platform.calls.clear()
    invoke(offline, "get_experiment_result", {"run_id": rid})
    invoke(offline, "get_job_status", {"run_id": rid})
    assert offline.platform.count() == 0


# ------------------------------------------------------------------ comparison summary (payload.benchmark)
def _bench(n_instruments: int = 5) -> dict:
    """FinanceModel's ``payload.benchmark`` shape (finplan_model.jobs.comparison)."""
    names = [f"I{i:03d}" for i in range(n_instruments)]
    w = {k: round(0.9 / n_instruments, 6) for k in names}

    def row(name, role, tr):
        return {
            "strategy": name, "role": role, "primary": role == "optimizer", "solution_status": "optimal" if role == "optimizer" else "not_applicable",
            "metrics": {"total_return": tr, "cagr": tr * 1.1, "ann_volatility": 0.12, "sharpe": None if name == "cash" else 0.8, "max_drawdown": 0.07, "turnover": 19.6, "transaction_cost": 392.0, "transaction_cost_fraction": 0.00392},
            "final_weights": {} if name == "cash" else w, "final_cash_weight": 1.0 if name == "cash" else 0.1,
            "average_weights": {} if name == "cash" else w, "average_cash_weight": 1.0 if name == "cash" else 0.1,
        }

    return {
        "schema": "finplan.benchmark_comparison/1", "primary_strategy": "mean_variance",
        "evaluation_window": {"start": "2024-01-02", "end": "2025-12-31", "sessions": 502}, "periods_per_year": 252,
        "base_currency": "USD", "initial_capital": 100000.0, "risk_free": {"annual_rate": 0.0, "source": "configured_cash_rate"},
        "units": {"turnover": "multiple of initial capital; cumulative traded notional (buys + sells) / initial capital", "max_drawdown": "positive fraction", "transaction_cost": "base_currency amount"},
        "strategies": [row("mean_variance", "optimizer", 0.21), row("cash", "control", 0.0), row("buy_and_hold", "control", 0.18), row("equal_weight", "control", 0.17)],
    }


def test_exp16_benchmark_comparison_table_and_optimizer_weights(offline, invoke):
    rid = _submitted(offline, invoke)
    offline.jobs.finish(rid, payload_extra={"benchmark": _bench()})
    resp = invoke(offline, "get_experiment_result", {"run_id": rid})
    assert not is_error(resp), resp
    c = resp["comparison"]
    assert [r["strategy"] for r in c["rows"]] == ["mean_variance", "cash", "buy_and_hold", "equal_weight"]
    mv = c["rows"][0]
    assert mv["role"] == "optimizer" and mv["total_return"] == 0.21 and mv["turnover"] == 19.6 and mv["transaction_cost"] == 392.0 and mv["max_drawdown"] == 0.07
    assert c["rows"][1]["sharpe"] is None and c["rows"][1]["role"] == "control"
    assert c["primary_weights"]["strategy"] == "mean_variance" and len(c["primary_weights"]["final"]) == 5 and c["primary_weights"]["final_cash"] == 0.1
    assert c["evaluation_window"]["sessions"] == 502 and c["risk_free"]["source"] == "configured_cash_rate" and c["base_currency"] == "USD"
    assert "turnover" in c["units"] and "truncated" not in resp
    assert resp["payload"]["benchmark"]["strategies"][2]["final_weights"]  # the full block passes through
    assert validate_document(resp, "tools/get-experiment-result-response").valid and response_leaks(resp) == []


def test_exp16_no_benchmark_block_no_comparison(offline, invoke):
    rid = _submitted(offline, invoke)
    offline.jobs.finish(rid)
    assert "comparison" not in invoke(offline, "get_experiment_result", {"run_id": rid})


def test_exp16_comparison_ignores_unreviewed_values():
    from finplan_tools.tools.get_experiment_result import comparison_summary

    bench = _bench(2)
    bench["strategies"].append({"strategy": "not a/valid name", "metrics": {}})
    bench["strategies"][0]["metrics"]["sharpe"] = float("nan")
    bench["strategies"][0]["metrics"]["note"] = "free text"
    bench["strategies"][0]["final_weights"]["../x"] = 0.5
    c = comparison_summary(bench)
    assert [r["strategy"] for r in c["rows"]] == ["mean_variance", "cash", "buy_and_hold", "equal_weight"]
    assert c["rows"][0]["sharpe"] is None and "note" not in c["rows"][0] and "../x" not in c["primary_weights"]["final"]
    assert comparison_summary({"strategies": "x"}) is None and comparison_summary(None) is None


def test_exp16_size_pressure_keeps_the_table():
    from types import SimpleNamespace

    from finplan_tools.tools.get_experiment_result import get_experiment_result
    from finplan_tools_testing._base import fixture

    result = fixture("job-result", "succeeded-optimal")
    result["payload"]["benchmark"] = _bench(60)
    jobs = SimpleNamespace(get_job_result=lambda run_id, meta: result)
    ctx = SimpleNamespace(jobs=jobs, meta=None, limits=ToolLimits.from_document({"response_max_bytes": 4096, "summary_top_n": 5}))
    doc = get_experiment_result(ctx, {"run_id": result["run_id"]})
    assert doc["truncated"] is True and "benchmark" not in doc["payload"] and "proposed_allocation" not in doc["payload"]
    assert len(doc["comparison"]["rows"]) == 4 and len(json.dumps(doc)) < 4096
    pw = doc["comparison"]["primary_weights"]
    assert pw["top_n"] == 5 and len(pw["final"]) == 6 and pw["final"]["other"] > 0
    assert validate_document(doc, "tools/get-experiment-result-response").valid
