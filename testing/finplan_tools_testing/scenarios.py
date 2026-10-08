"""Synthetic fixture scenarios (task 3.2) seeded into the offline mocks.

Every scenario seeds an :class:`~finplan_tools_testing.runtime.Offline` world and returns a
:class:`Scenario` naming the tool, the request(s) and the expected outcome. All data is synthetic
(``synthetic: true``), derived from the pinned contract package's own fixtures. Market data uses the
S&P 500 tracking-ETF daily dataset ``finance/etf-daily/SPY`` (daily completed observations only),
never the index level or the constituent universe, and no value comes from a real provider.

``md_yfinance_lineage_shape`` is the one scenario whose snapshot *lineage* names ``yfinance``: it
checks that lineage is surfaced unchanged. Its observations are synthetic and it is listed in
:data:`REAL_PROVIDER_LINEAGE_ALLOWED`, the explicit allow-list the fixture-provenance build check
(task 5.2b) must honour; any other fixture naming a real provider fails that check.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from finplan_tools.core.contracts import contract_version

from .mock_jobs import research_request
from .runtime import Offline

ETF_DATASET = "finance/etf-daily/SPY"
MOCK_PROVIDERS = ("fixture", "mock")
REAL_PROVIDER_LINEAGE_ALLOWED = frozenset({"md_yfinance_lineage_shape"})


@dataclass
class Scenario:
    name: str
    tool: str
    requests: list[dict[str, Any]]
    expect: dict[str, Any]
    ids: dict[str, str] = field(default_factory=dict)
    #: (schema name, document) pairs the scenario seeded; each must validate.
    documents: list[tuple[str, Any]] = field(default_factory=list)


def _plan_world(o: Offline, *, synthetic_portfolio: bool = True) -> dict[str, str]:
    pf = o.platform.add_portfolio(synthetic=synthetic_portfolio)
    pl, pv = o.platform.add_plan(pf)
    return {"portfolio_id": pf, "plan_id": pl, "plan_version_id": pv}


def _override(ids: dict[str, str], revision: int, key: str, **content_overrides: Any) -> dict[str, Any]:
    from .mock_platform import MockPlatform

    content = MockPlatform.default_content()
    content.update(content_overrides)
    return {
        "plan_id": ids["plan_id"],
        "parent_plan_version_id": ids["plan_version_id"],
        "expected_revision": revision,
        "idempotency_key": key,
        "domain": "finance",
        "domain_schema_version": "1.0",
        "content": content,
        "synthetic": True,
    }


def _experiment(o: Offline, job_type: str = "fixture_optimizer", key: str = "exp-key-0001", **kw: Any) -> tuple[dict[str, Any], str]:
    sid = o.platform.add_snapshot()
    req = research_request(job_type=job_type, input_snapshot_id=sid, idempotency_key=key, **kw)
    return req, sid


# ------------------------------------------------------------------ experiments
def duplicate(o: Offline) -> Scenario:
    req, sid = _experiment(o)
    return Scenario("duplicate", "submit_experiment", [req, dict(req)], {"runs": 1, "same_run_id": True}, {"input_snapshot_id": sid})


def key_reuse(o: Offline) -> Scenario:
    req, sid = _experiment(o)
    other = dict(req, evaluation_window={"start": "2026-01-05", "end": "2026-01-09"})
    return Scenario("key_reuse", "submit_experiment", [req, other], {"second_code": "IDEMPOTENCY_KEY_REUSED", "retryable": False}, {"input_snapshot_id": sid})


def _finished(o: Offline, name: str, completion: str, solution: str | None, **kw: Any) -> Scenario:
    req, sid = _experiment(o, key=f"{name}-key")
    o.jobs.idempotency.clear()
    from finplan_tools.backends.jobs import JobClient
    from finplan_tools.core.transport import CallMeta

    run = JobClient(o.jobs).submit_job(dict(req, idempotency_key=f"seed-{name}"), CallMeta("seed-correlation-0001", contract_version()))
    o.jobs.finish(run["run_id"], completion, solution, **kw)
    o.jobs.calls.clear()
    doc = o.jobs.runs[run["run_id"]]["result"]
    return Scenario(name, "get_experiment_result", [{"run_id": run["run_id"], "synthetic": True}], {"completion_status": completion, "solution_status": solution}, {"run_id": run["run_id"], "input_snapshot_id": sid}, [("job-result", doc)])


def infeasible(o: Offline) -> Scenario:
    return _finished(o, "infeasible", "succeeded", "infeasible")


def no_effect(o: Offline) -> Scenario:
    return _finished(o, "no_effect", "succeeded", "no_effect")


def failed(o: Offline) -> Scenario:
    return _finished(o, "failed", "failed", None)


def timed_out_partial(o: Offline) -> Scenario:
    s = _finished(o, "timed_out_partial", "timed_out", None, artifacts_complete=False)
    s.expect["artifacts_complete"] = False
    return s


def budget_cpu_over(o: Offline) -> Scenario:
    req, sid = _experiment(o, job_type="cpu_backtest")
    return Scenario("budget_cpu_over", "submit_experiment", [req], {"code": "BUDGET_EXCEEDED", "budget_category": "cpu_research", "estimate": 1.40, "limit": 1.00, "runs": 0}, {"input_snapshot_id": sid})


def budget_gpu_within(o: Offline) -> Scenario:
    req, sid = _experiment(o, job_type="gpu_training")
    return Scenario("budget_gpu_within", "submit_experiment", [req], {"state": "awaiting_approval", "budget_category": "gpu", "estimate": 3.00, "limit": 5.00, "runs": 1}, {"input_snapshot_id": sid})


def budget_zero_limit_category(o: Offline) -> Scenario:
    req, sid = _experiment(o, job_type="explain_batch")
    return Scenario("budget_zero_limit_category", "submit_experiment", [req], {"code": "BUDGET_EXCEEDED", "budget_category": "bedrock_explanations", "limit": 0.0, "runs": 0}, {"input_snapshot_id": sid})


def schema_upgrade(o: Offline) -> Scenario:
    """A request built under 1.0.0 reaching a 1.x release, and a snapshot without the later lineage fields."""
    ids = _plan_world(o)
    sid = o.platform.add_snapshot(lineage={"provider": "mock", "retrieved_at": "2026-01-10T13:00:00Z"})
    return Scenario("schema_upgrade", "get_plan_version", [{"plan_version_id": ids["plan_version_id"], "contract_version": "1.0.0", "synthetic": True}], {"accepted": True}, {**ids, "input_snapshot_id": sid}, [("input-snapshot", o.platform.snapshots[sid])])


def unsupported_major(o: Offline) -> Scenario:
    ids = _plan_world(o)
    return Scenario("unsupported_major", "get_plan_version", [{"plan_version_id": ids["plan_version_id"], "contract_version": "2.0.0", "synthetic": True}], {"code": "UNSUPPORTED_CONTRACT_VERSION", "served_contract_majors": [1], "calls": 0}, ids)


# ------------------------------------------------------------------ plans
def conflict(o: Offline) -> Scenario:
    ids = _plan_world(o)
    return Scenario("conflict", "create_override_version", [_override(ids, 99, "ovr-key-0001")], {"code": "CONFLICT", "retryable": False}, ids)


def override_no_effect(o: Offline) -> Scenario:
    ids = _plan_world(o)
    return Scenario("override_no_effect", "create_override_version", [_override(ids, 1, "ovr-key-0002")], {"no_effect": True}, ids)


def non_synthetic_portfolio(o: Offline) -> Scenario:
    ids = _plan_world(o, synthetic_portfolio=False)
    return Scenario("non_synthetic_portfolio", "create_override_version", [_override(ids, 1, "ovr-key-0003", base_currency="USD")], {"code": "OPERATION_NOT_PERMITTED"}, ids)


# ------------------------------------------------------------------ market data
def _refresh_request(key: str = "md-key-0001") -> dict[str, Any]:
    return {"dataset_id": ETF_DATASET, "instrument_ids": ["SPY"], "start_date": "2026-01-02", "end_date": "2026-01-09", "granularity": "daily", "idempotency_key": key, "synthetic": True}


def md_rate_limited(o: Offline) -> Scenario:
    o.platform.fail_next("run_ingestion", "RATE_LIMITED", retryable=True, details={"reason": "provider_throttled_after_backoff"})
    return Scenario("md_rate_limited", "refresh_market_data", [_refresh_request()], {"code": "RATE_LIMITED", "retryable": True, "ingestion_calls": 1})


def md_partial_response(o: Offline) -> Scenario:
    sid = o.platform.add_snapshot(status="approved", quality_flags=["partial_response", "missing_sessions"], observed_end="2026-01-07", quality_details={"missing_sessions": ["2026-01-08", "2026-01-09"]})
    return Scenario("md_partial_response", "query_market_data", [{"input_snapshot_id": sid, "synthetic": True}], {"partial": True, "quality_flags": ["partial_response", "missing_sessions"]}, {"input_snapshot_id": sid}, [("input-snapshot", o.platform.snapshots[sid])])


def md_empty_response(o: Offline) -> Scenario:
    o.platform.ingestion_quality_flags = ["empty_response", "no_new_observations"]
    return Scenario("md_empty_response", "refresh_market_data", [_refresh_request("md-key-0002")], {"quality_flags": ["empty_response", "no_new_observations"]})


def md_committed_snapshot(o: Offline) -> Scenario:
    sid = o.platform.add_snapshot(status="committed")
    return Scenario("md_committed_snapshot", "query_market_data", [{"input_snapshot_id": sid, "synthetic": True}], {"code": "PRECONDITION_FAILED", "status": "committed"}, {"input_snapshot_id": sid}, [("input-snapshot", o.platform.snapshots[sid])])


def md_yfinance_lineage_shape(o: Offline) -> Scenario:
    lineage = {"provider": "yfinance", "provider_library": "yfinance", "library_version": "1.7.0", "retrieved_at": "2026-01-10T13:00:00Z", "calendar_version": "fixture-synthetic-v1"}
    sid = o.platform.add_snapshot(status="approved", lineage=lineage)
    return Scenario("md_yfinance_lineage_shape", "query_market_data", [{"input_snapshot_id": sid, "synthetic": True}], {"lineage": lineage, "observations_synthetic": True}, {"input_snapshot_id": sid}, [("input-snapshot", o.platform.snapshots[sid])])


def md_intraday_rejected(o: Offline) -> Scenario:
    return Scenario("md_intraday_rejected", "refresh_market_data", [dict(_refresh_request("md-key-0003"), granularity="intraday")], {"code": "VALIDATION_FAILED", "ingestion_calls": 0})


SCENARIOS: dict[str, Callable[[Offline], Scenario]] = {
    f.__name__: f
    for f in (
        duplicate,
        key_reuse,
        infeasible,
        no_effect,
        failed,
        timed_out_partial,
        budget_cpu_over,
        budget_gpu_within,
        budget_zero_limit_category,
        schema_upgrade,
        unsupported_major,
        conflict,
        override_no_effect,
        non_synthetic_portfolio,
        md_rate_limited,
        md_partial_response,
        md_empty_response,
        md_committed_snapshot,
        md_yfinance_lineage_shape,
        md_intraday_rejected,
    )
}

__all__ = ["ETF_DATASET", "MOCK_PROVIDERS", "REAL_PROVIDER_LINEAGE_ALLOWED", "SCENARIOS", "Scenario"]
