"""Consumer-side contract tests of the 12 tools (tasks 4-7).

* every valid request fixture of the pinned contract package passes the tool's input validation, and
  every invalid one is refused before any producer call;
* every body a tool sends validates against the schema the real producer validates
  (FinanceModel ``job-submission``; platform tool request schemas with the path id added);
* every synthetic scenario runs end to end through its tool with the expected outcome;
* every success response validates against the tool's pinned output schema.
"""

from __future__ import annotations

import json

import pytest

from finplan_contracts.schemas import contracts_root

from finplan_tools.core.contracts import validate_document
from finplan_tools.core.registry import CATALOG
from finplan_tools_testing.mock_platform import MockPlatform
from finplan_tools_testing.runtime import offline_runtime
from finplan_tools_testing.scenarios import SCENARIOS

FIX = contracts_root() / "fixtures" / "tools"


def is_error(resp, code=None):
    return {"code", "message", "retryable", "correlation_id"} <= set(resp) and (code is None or resp["code"] == code)


def _fixtures(kind):
    out = []
    for tool, entry in CATALOG.items():
        d = FIX / entry.input_schema.removeprefix("tools/") / kind
        out += [pytest.param(tool, p, id=f"{tool}-{p.stem}") for p in sorted(d.glob("*.json"))]
    return out


@pytest.mark.parametrize("tool,path", _fixtures("valid"))
def test_valid_request_fixtures_pass_input_validation(tool, path, invoke):
    o = offline_runtime("beta")
    resp = invoke(o, tool, json.loads(path.read_text(encoding="utf-8")))
    if is_error(resp, "VALIDATION_FAILED") and resp["details"].get("pointer") == "/next_token":
        return  # schema-valid, but continuation tokens are opaque and bound to the issuing tool (D7)
    assert not is_error(resp, "VALIDATION_FAILED") and not is_error(resp, "INVALID_IDENTIFIER"), resp


@pytest.mark.parametrize("tool,path", _fixtures("invalid"))
def test_invalid_request_fixtures_refused_without_calls(tool, path, invoke):
    o = offline_runtime("beta")
    resp = invoke(o, tool, json.loads(path.read_text(encoding="utf-8")))
    assert is_error(resp) and resp["code"] in ("VALIDATION_FAILED", "INVALID_IDENTIFIER", "IMMUTABLE_RECORD"), resp
    assert o.platform.count() == 0 and o.jobs.count() == 0


# ---------------------------------------------------------------- producer request conformance
@pytest.fixture
def world():
    o = offline_runtime("beta")
    pf = o.platform.add_portfolio()
    pl, pv = o.platform.add_plan(pf)
    return o, pl, pv


def test_producer_bodies_conform(world, invoke):
    o, pl, pv = world
    sid = o.platform.add_snapshot()
    req = json.loads((FIX / "submit-experiment-request" / "valid" / "research.json").read_text())
    invoke(o, "submit_experiment", dict(req, input_snapshot_id=sid))
    invoke(o, "refresh_market_data", {"dataset_id": "finance/etf-daily/SPY", "start_date": "2026-01-02", "end_date": "2026-01-09", "granularity": "daily", "idempotency_key": "md-key-0001"})
    content = MockPlatform.default_content()
    content["allocation"]["cash_weight"] = 0.3
    content["allocation"]["weights"][0]["weight"] = 0.7
    child = invoke(o, "create_override_version", {"plan_id": pl, "parent_plan_version_id": pv, "expected_revision": 1, "idempotency_key": "ovr-key-0001", "domain": "finance", "domain_schema_version": "1.0", "content": content})
    invoke(o, "validate_plan_version", {"plan_version_id": child["plan_version_id"], "idempotency_key": "val-key-0001"})
    invoke(o, "publish_plan_version", {"plan_id": pl, "plan_version_id": child["plan_version_id"], "expected_revision": 2, "idempotency_key": "pub-key-0001"})
    checked = 0
    for c in o.jobs.calls:
        if c.op == "submit_job":
            assert validate_document(c.body, "job-submission").valid
            checked += 1
    schema = {"run_ingestion": "tools/refresh-market-data-request", "create_plan_version": "tools/create-override-version-request", "validate_plan_version": "tools/validate-plan-version-request", "publish_plan_version": "tools/publish-plan-version-request"}
    for c in o.platform.calls:
        if c.op in schema:
            body = dict(c.body)
            # the platform adds the path identifier before validating (``req[...] = path id``)
            if c.op == "validate_plan_version":
                body["plan_version_id"] = c.path.split("/")[2]
            if c.op in ("create_plan_version", "publish_plan_version"):
                body["plan_id"] = c.path.split("/")[2]
            res = validate_document(body, schema[c.op])
            assert res.valid, (c.op, [i.message for i in res.issues])
            checked += 1
    assert checked == 6
    for c in o.platform.calls + o.jobs.calls:  # caller block travels in headers, never in bodies
        assert not (isinstance(c.body, dict) and "caller" in c.body)


# ---------------------------------------------------------------- scenarios end to end
@pytest.mark.parametrize("name", sorted(SCENARIOS))
def test_scenarios_end_to_end(name, invoke):
    o = offline_runtime("beta")
    s = SCENARIOS[name](o)
    resps = [invoke(o, s.tool, r) for r in s.requests]
    last, exp = resps[-1], s.expect
    if "code" in exp:
        assert is_error(last, exp["code"]), last
    if "second_code" in exp:
        assert is_error(resps[1], exp["second_code"]) and resps[1]["retryable"] is exp["retryable"]
    if "runs" in exp:
        assert len(o.jobs.runs) == exp["runs"]
    if exp.get("same_run_id"):
        assert resps[0]["run_id"] == resps[1]["run_id"]
    for k in ("completion_status", "solution_status", "state", "no_effect", "partial", "artifacts_complete", "quality_flags"):
        if k in exp and "code" not in exp:
            got = last.get(k) if k != "quality_flags" or "quality_flags" in last else last["snapshot"]["quality_flags"]
            assert got == exp[k], (k, last)
    if "lineage" in exp:
        assert last["snapshot"]["lineage"] == exp["lineage"]
    if "ingestion_calls" in exp:
        assert o.platform.count("run_ingestion") == exp["ingestion_calls"]
    if "calls" in exp:
        assert o.platform.count() + o.jobs.count() == exp["calls"]
    if exp.get("accepted"):
        assert not is_error(last)
    for r in resps:
        if not is_error(r):
            assert validate_document(r, CATALOG[s.tool].output_schema).valid
