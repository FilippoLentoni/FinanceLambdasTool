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
