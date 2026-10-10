"""describe_capabilities (tasks 4.1, 4.2) and the market-data tools (tasks 5.1, 5.2, 5.2a, 5.3).

Every tool runs through the real handler, pipeline and producer clients against the in-process mock
platform; "no call" assertions are mock call counts. All market data is synthetic.
"""

from __future__ import annotations

import json

import pytest

from finplan_tools.core.artifacts import response_leaks
from finplan_tools.core.contracts import contract_version, store, validate_document
from finplan_tools.core.idempotency import DERIVED_KEY_PATTERN
from finplan_tools.core.registry import CATALOG
from finplan_tools_testing.scenarios import ETF_DATASET, SCENARIOS

EXPERIMENT_TOOLS = {name for name, entry in CATALOG.items() if "financemodel" in entry.producers}
PLATFORM_TOOLS = {n for n, e in CATALOG.items() if "financialplanning" in e.producers}


def is_error(resp, code=None):
    return {"code", "message", "retryable", "correlation_id"} <= set(resp) and (code is None or resp["code"] == code)


def refresh_req(key="md-key-0001", **kw):
    req = {"dataset_id": ETF_DATASET, "instrument_ids": ["SPY"], "start_date": "2026-01-02", "end_date": "2026-01-09", "granularity": "daily", "idempotency_key": key, "synthetic": True}
    req.update(kw)
    return req


# =================================================================== describe_capabilities
def test_cap01_beta_listing_every_catalog_tool(offline, invoke):
    resp = invoke(offline, "describe_capabilities", {})
    assert validate_document(resp, "tools/describe-capabilities-response").valid
    assert resp["environment"] == "beta" and resp["release_id"] == offline.runtime.settings.release_id
    assert resp["contract_version"] == contract_version() and resp["served_contract_majors"] == [1]
    assert [t["name"] for t in resp["tools"]] == list(CATALOG)
    for t in resp["tools"]:
        e = CATALOG[t["name"]]
        assert t["input_schema_id"] == store().get(e.input_schema).schema["$id"]
        assert t["output_schema_id"] == store().get(e.output_schema).schema["$id"]
        assert t["state_changing"] is e.state_changing and t["available"] is True
    assert offline.platform.count() == 0 and offline.jobs.count() == 0


def test_cap02_model_absent_marks_only_experiment_tools(offline, invoke):
    offline.remove_model()
    tools = {t["name"]: t for t in invoke(offline, "describe_capabilities", {})["tools"]}
    for name, t in tools.items():
        if name in EXPERIMENT_TOOLS:
            assert t["available"] is False and t["unavailable_reason"] == "DEPENDENCY_UNAVAILABLE"
        else:
            assert t["available"] is True and "unavailable_reason" not in t


def test_cap02_incompatible_platform_major(offline_factory, invoke):
    o = offline_factory("gamma", platform_majors=[2])
    tools = {t["name"]: t for t in invoke(o, "describe_capabilities", {})["tools"]}
    for name in PLATFORM_TOOLS:
        assert tools[name]["available"] is False and tools[name]["unavailable_reason"] == "UNSUPPORTED_CONTRACT_VERSION", name
    assert tools["describe_capabilities"]["available"] is True
    assert tools["get_job_status"]["available"] is True  # FinanceModel serves the pinned major


def test_cap02_availability_follows_manifests_not_code(offline, invoke):
    offline.set_param("financemodel", "release", "manifest", {"served_contract_majors": []})
    tools = {t["name"]: t for t in invoke(offline, "describe_capabilities", {})["tools"]}
    assert tools["get_job_status"]["unavailable_reason"] == "DEPENDENCY_UNAVAILABLE"


def test_cap03_limits_from_tool_limits_defaults(offline, invoke):
    lim = invoke(offline, "describe_capabilities", {})["limits"]
    assert lim["max_estimated_usd_per_call"] == {"bedrock_explanations": 0.0, "cpu_research": 1.0, "gpu": 5.0, "platform_infra": 0.0, "reserve": 0.0}
    assert lim["gpu_runs_require_user_approval"] is True and "approval" in lim["approval_note"]
    assert lim["response_max_bytes"] == 65536


def test_cap03_limits_follow_configuration(offline_factory, invoke):
    o = offline_factory("beta", tool_limits={"response_max_bytes": 32768, "max_estimated_usd_per_call": {"cpu_research": 0.5}})
    lim = invoke(o, "describe_capabilities", {})["limits"]
    assert lim["max_estimated_usd_per_call"] == {"cpu_research": 0.5} and lim["response_max_bytes"] == 32768


def test_cap03_daily_only_granularity_never_intraday(offline, invoke):
    lim = invoke(offline, "describe_capabilities", {})["limits"]
    assert lim["market_data_granularities"] == ["daily"] and "intraday" not in json.dumps(lim)
    assert lim["experiment_types_declared"] is False and lim["experiment_types"] == []


@pytest.mark.parametrize("env", ["beta", "gamma", "prod"])
def test_cap04_leak_check_every_environment(offline_factory, invoke, env):
    o = offline_factory(env)
    resp = invoke(o, "describe_capabilities", {"synthetic": True})
    assert not is_error(resp) and resp["synthetic"] is True
    assert response_leaks(resp) == []
    text = json.dumps(resp)
    for needle in ("arn:", "amazonaws.com", "execute-api", "/finplan/", "role"):
        assert needle not in text


def test_cap_needs_no_idempotency_key_and_rejects_unknown_fields(offline, invoke):
    assert not is_error(invoke(offline, "describe_capabilities", {}))
    assert is_error(invoke(offline, "describe_capabilities", {"idempotency_key": "abc-12345678"}), "VALIDATION_FAILED")


def test_cap_without_release_id_is_internal(offline_factory, invoke):
    o = offline_factory("beta", release_id=None)
    assert is_error(invoke(o, "describe_capabilities", {}), "INTERNAL")


# =================================================================== refresh_market_data
def test_mkt01_fixture_refresh_returns_platform_snapshot(offline, invoke, no_network):
    resp = invoke(offline, "refresh_market_data", refresh_req())
    assert not is_error(resp), resp
    snap = resp["snapshot"]
    assert resp["input_snapshot_id"] == snap["input_snapshot_id"] and snap["input_snapshot_id"] in offline.platform.snapshots
    assert snap["dataset"]["dataset_id"] == ETF_DATASET
    for k in ("source_timestamps", "coverage", "quality_flags", "manifest_checksum", "lineage"):
        assert k in snap
    assert snap["lineage"]["retrieved_at"] and resp["coverage_complete"] is True
    assert offline.platform.ops() == ["run_ingestion"]
    body = offline.platform.calls[0].body
    assert DERIVED_KEY_PATTERN.match(body["idempotency_key"]) and body["granularity"] == "daily"
    assert validate_document(dict(body, idempotency_key="placeholder-key-1"), "tools/refresh-market-data-request").valid


def test_mkt02_duplicate_refresh_one_ingestion(offline, invoke):
    a = invoke(offline, "refresh_market_data", refresh_req())
    b = invoke(offline, "refresh_market_data", refresh_req())
    assert a["input_snapshot_id"] == b["input_snapshot_id"]
    assert len(offline.platform.snapshots) == 1  # the platform recorded one ingestion


def test_mkt02_same_key_other_body_is_key_reused(offline, invoke):
    invoke(offline, "refresh_market_data", refresh_req())
    resp = invoke(offline, "refresh_market_data", refresh_req(start_date="2026-01-05"))
    assert is_error(resp, "IDEMPOTENCY_KEY_REUSED") and len(offline.platform.snapshots) == 1


def test_mkt03_intraday_rejected_without_ingestion(offline, invoke):
    resp = invoke(offline, "refresh_market_data", refresh_req(granularity="intraday"))
    assert is_error(resp, "VALIDATION_FAILED") and resp["details"].get("pointer") == "/granularity"
    assert offline.platform.count() == 0


def test_mkt03_platform_capability_rejection_passed_through(offline, invoke):
    offline.platform.declared_granularities = []  # provider declares nothing usable
    resp = invoke(offline, "refresh_market_data", refresh_req())
    assert is_error(resp, "VALIDATION_FAILED") and resp["details"]["pointer"] == "/granularity"
    assert offline.platform.count("run_ingestion") == 1 and not offline.platform.snapshots


@pytest.mark.parametrize("producer_retryable", [False, True])
def test_mkt04_budget_exceeded_never_retryable_never_retried(offline, invoke, producer_retryable):
    offline.platform.fail_next("run_ingestion", "BUDGET_EXCEEDED", retryable=producer_retryable)
    resp = invoke(offline, "refresh_market_data", refresh_req())
    assert is_error(resp, "BUDGET_EXCEEDED") and resp["retryable"] is False
    assert offline.platform.count("run_ingestion") == 1 and not offline.platform.snapshots


def test_mkt08_rate_limited_passed_through_once(offline, invoke):
    s = SCENARIOS["md_rate_limited"](offline)
    resp = invoke(offline, s.tool, s.requests[0])
    assert is_error(resp, "RATE_LIMITED") and resp["retryable"] is True
    assert offline.platform.count("run_ingestion") == s.expect["ingestion_calls"] and not offline.platform.snapshots


def test_mkt08_dependency_unavailable_keeps_retryable(offline, invoke):
    offline.platform.fail_next("run_ingestion", "DEPENDENCY_UNAVAILABLE", retryable=True)
    resp = invoke(offline, "refresh_market_data", refresh_req())
    assert is_error(resp, "DEPENDENCY_UNAVAILABLE") and resp["retryable"] is True
    assert offline.platform.count("run_ingestion") == 1


def test_mkt08_partial_provider_response_flags_unchanged(offline, invoke):
    offline.platform.ingestion_quality_flags = ["partial_response", "missing_sessions"]
    offline.platform.ingestion_coverage_end = "2026-01-07"
    resp = invoke(offline, "refresh_market_data", refresh_req())
    assert resp["quality_flags"] == ["partial_response", "missing_sessions"] == resp["snapshot"]["quality_flags"]
    assert resp["coverage_complete"] is False


def test_mkt08_empty_provider_response_scenario(offline, invoke):
    s = SCENARIOS["md_empty_response"](offline)
    resp = invoke(offline, s.tool, s.requests[0])
    assert resp["quality_flags"] == s.expect["quality_flags"]


# =================================================================== query_market_data
def test_mkt05_query_etf_daily_snapshot(offline, invoke):
    sid = offline.platform.add_snapshot()
    resp = invoke(offline, "query_market_data", {"input_snapshot_id": sid, "synthetic": True})
    assert not is_error(resp), resp
    assert resp["observation_kinds"] == ["completed_daily"]
    assert resp["snapshot"]["dataset"]["dataset_id"] == "finance/etf-daily/SPY"
    assert resp["snapshot"]["manifest_checksum"].startswith("sha256:") and resp["snapshot"]["coverage"] == {"start": "2026-01-02", "end": "2026-01-09"}
    assert resp["instruments"][0]["instrument_id"] == "SPY" and resp["instruments"][0]["observation_count"] == 6
    assert [r["kind"] for r in resp["data_refs"]] == ["snapshot_payload"]
    assert resp["partial"] is False and "observations" not in resp
    text = json.dumps(resp)
    assert "index-level" not in text and "universe" not in text
    assert offline.platform.ops() == ["read_snapshot_observations"]


def test_mkt05_filters_sent_as_the_platform_declares_them(offline, invoke):
    sid = offline.platform.add_snapshot()
    resp = invoke(offline, "query_market_data", {"input_snapshot_id": sid, "instrument_ids": ["SPY"], "start_date": "2026-01-05", "end_date": "2026-01-07"})
    assert not is_error(resp)
    assert offline.platform.observation_queries == [{"page_size": 1, "instrument_id": ["SPY"], "start_date": "2026-01-05", "end_date": "2026-01-07"}]
    assert resp["instruments"][0]["observation_count"] == 3


def test_mkt05_instrument_without_observations(offline, invoke):
    sid = offline.platform.add_snapshot()
    resp = invoke(offline, "query_market_data", {"input_snapshot_id": sid, "instrument_ids": ["QQQ"]})
    assert resp["instruments"] == []


def test_mkt06_range_before_coverage_is_partial_without_fabrication(offline, invoke):
    sid = offline.platform.add_snapshot(start="2026-01-05", end="2026-01-09")
    resp = invoke(offline, "query_market_data", {"input_snapshot_id": sid, "start_date": "2026-01-01"})
    assert resp["partial"] is True
    assert resp["snapshot"]["coverage"]["start"] == "2026-01-05"
    assert resp["instruments"][0]["first_date"] >= "2026-01-05" and resp["instruments"][0]["observation_count"] == 5


def test_mkt06_partial_provider_snapshot_scenario(offline, invoke):
    s = SCENARIOS["md_partial_response"](offline)
    resp = invoke(offline, s.tool, s.requests[0])
    assert resp["partial"] is True and resp["snapshot"]["quality_flags"] == s.expect["quality_flags"]
    assert resp["instruments"][0]["last_date"] == "2026-01-07"


def test_mkt06_inverted_range_rejected_without_call(offline, invoke):
    sid = offline.platform.add_snapshot()
    resp = invoke(offline, "query_market_data", {"input_snapshot_id": sid, "start_date": "2026-01-09", "end_date": "2026-01-02"})
    assert is_error(resp, "VALIDATION_FAILED") and offline.platform.count() == 0


def test_mkt07_unknown_snapshot_not_found(offline, invoke):
    resp = invoke(offline, "query_market_data", {"input_snapshot_id": "snap_01KM9999999999999999999999"})
    assert is_error(resp, "NOT_FOUND")


def test_mkt07_prod_snapshot_from_gamma_not_found(offline_factory, invoke):
    prod, gamma = offline_factory("prod"), offline_factory("gamma")
    sid = prod.platform.add_snapshot()
    resp = invoke(gamma, "query_market_data", {"input_snapshot_id": sid})
    assert is_error(resp, "NOT_FOUND") and prod.platform.count() == 0


def test_mkt11_lineage_surfaced_unchanged(offline, invoke):
    s = SCENARIOS["md_yfinance_lineage_shape"](offline)
    resp = invoke(offline, s.tool, s.requests[0])
    assert resp["snapshot"]["lineage"] == s.expect["lineage"]


@pytest.mark.parametrize("status", ["committed", "expired"])
def test_mkt11_not_approved_is_precondition_failed(offline, invoke, status):
    sid = offline.platform.add_snapshot(status=status)
    resp = invoke(offline, "query_market_data", {"input_snapshot_id": sid})
    assert is_error(resp, "PRECONDITION_FAILED") and resp["details"]["status"] == status
    assert "instruments" not in resp


def test_query_without_observation_route_returns_metadata_only(offline, invoke):
    sid = offline.platform.add_snapshot()
    offline.platform.missing_routes.add("read_snapshot_observations")
    resp = invoke(offline, "query_market_data", {"input_snapshot_id": sid})
    assert not is_error(resp), resp
    assert resp["observation_summary"]["unavailable_reason"] == "DEPENDENCY_UNAVAILABLE"
    assert resp["instruments"] == [] and resp["snapshot"]["input_snapshot_id"] == sid
    assert offline.platform.ops() == ["read_snapshot_observations", "get_snapshot"]


def test_query_unreachable_platform_is_dependency_unavailable(offline, invoke):
    sid = offline.platform.add_snapshot()
    offline.platform.unreachable = True
    resp = invoke(offline, "query_market_data", {"input_snapshot_id": sid})
    assert is_error(resp, "DEPENDENCY_UNAVAILABLE") and offline.platform.count("get_snapshot") == 0


def test_query_foreign_next_token_rejected(offline, invoke):
    sid = offline.platform.add_snapshot()
    from finplan_tools.core.bounds import wrap_token

    other = wrap_token(tool="list_plan_versions", environment="beta", offset=3)  # issued by another tool
    resp = invoke(offline, "query_market_data", {"input_snapshot_id": sid, "next_token": other})
    assert is_error(resp, "VALIDATION_FAILED") and offline.platform.count() == 0
