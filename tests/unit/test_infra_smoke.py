"""REL-08 (task 9.5): the prod smoke calls, replayed offline against the mock producers, make zero
write calls and create no FinanceModel run. Also checks that the deployed suites are skipped offline."""

from __future__ import annotations

from tests.deployed_support import fixture_request
from tests.smoke.calls import SMOKE_TOOLS


def test_smoke_calls_are_read_only_against_the_mocks(offline_factory, invoke):
    from finplan_tools.core.registry import get_tool
    from finplan_tools.tools import load_all

    load_all()
    o = offline_factory("prod")
    ran = 0
    for tool in SMOKE_TOOLS:
        if get_tool(tool) is None:  # implemented by the tool task groups
            continue
        invoke(o, tool, fixture_request(tool), source="ci_test")
        ran += 1
    assert ran >= 1
    for producer in (o.platform, o.jobs):
        assert all(c.method == "GET" for c in producer.calls), [(c.op, c.method) for c in producer.calls]
    assert o.jobs.count("submit_job") == 0


def test_deployed_suites_skip_offline():
    import os

    assert not os.environ.get("FINPLAN_TARGET_ENV")
    from tests.deployed_support import deployed

    assert deployed.args[0] is True  # skip condition holds offline


def test_deployed_provenance_check_accepts_real_and_synthetic_snapshots(offline_factory, invoke):
    """Decision 26 (data parity): the deployed suites accept a REAL phase 2 snapshot (``yfinance``
    lineage, no ``synthetic`` flag) and a still-SYNTHETIC one in every environment, prod included; the
    tool must surface the platform's provenance unchanged. Snapshots here are in-memory mock data."""
    import pytest

    from finplan_tools.core.registry import get_tool
    from finplan_tools.tools import load_all
    from tests.deployed_support import check_snapshot_provenance

    load_all()
    if get_tool("query_market_data") is None:
        pytest.skip("query_market_data not registered")
    for env in ("beta", "gamma", "prod"):
        o = offline_factory(env)
        synthetic_sid = o.platform.add_snapshot()
        o.platform.snapshots[synthetic_sid]["synthetic"] = True
        real_sid = o.platform.add_snapshot(lineage={"provider": "yfinance", "provider_library": "yfinance", "library_version": "1.7.0", "retrieved_at": "2026-01-10T13:00:00Z"})
        o.platform.snapshots[real_sid].pop("synthetic", None)
        assert check_snapshot_provenance(invoke(o, "query_market_data", {"input_snapshot_id": synthetic_sid})) == "synthetic"
        assert check_snapshot_provenance(invoke(o, "query_market_data", {"input_snapshot_id": real_sid})) == "real"
        flagged = invoke(o, "query_market_data", {"input_snapshot_id": real_sid, "synthetic": True})
        assert check_snapshot_provenance(flagged, request_synthetic=True) == "real"
    # a real-looking snapshot naming a mock provider without the flag is a provenance defect
    with pytest.raises(AssertionError):
        check_snapshot_provenance({"snapshot": {"lineage": {"provider": "fixture"}}})
