"""Mock producers in producer mode (task 3.1, CS-10) and the synthetic scenarios (task 3.2).

Every mock answer is validated inside the mock against the contract schema the real producer uses
(``require_valid``), so driving every route here is the producer-mode conformance run.
"""

from __future__ import annotations

import json

import pytest

from finplan_tools.backends.jobs import JobClient
from finplan_tools.backends.platform import PlatformClient
from finplan_tools.core.contracts import contract_version, validate_document
from finplan_tools.core.errors import ToolError
from finplan_tools.core.transport import CallMeta
from finplan_tools_testing.mock_jobs import research_request
from finplan_tools_testing.mock_platform import PLAN_ROUTES
from finplan_tools_testing.runtime import offline_runtime
from finplan_tools_testing.scenarios import ETF_DATASET, MOCK_PROVIDERS, REAL_PROVIDER_LINEAGE_ALLOWED, SCENARIOS

META = CallMeta("corr-contract-0001", contract_version(), {"channel": "ci_test", "correlation_id": "corr-contract-0001"})


@pytest.fixture
def o():
    return offline_runtime("beta")


def test_every_platform_route_answers_conformant_documents(o):
    p = o.platform
    c = PlatformClient(p, p)
    pf = p.add_portfolio()
    pl, root = p.add_plan(pf)
    sid = p.add_snapshot()
    assert validate_document(c.get_plan(pl, META), "tools/get-plan-response").valid
    assert validate_document(c.get_plan_version(root, META), "tools/get-plan-version-response").valid
    assert validate_document(c.get_portfolio(pf, META), "portfolio").valid
    assert validate_document(c.get_snapshot(sid, META)["snapshot"], "input-snapshot").valid
    assert validate_document(c.read_snapshot_observations(sid, META), "api/read-snapshot-observations-response").valid
    content = p.default_content()
    content["allocation"]["cash_weight"] = 0.3  # sums to 0.9 -> invalid on validation
    child = c.create_plan_version(pl, {"parent_plan_version_id": root, "expected_revision": 1, "idempotency_key": "k-child", "domain": "finance", "domain_schema_version": "1.0", "content": content}, META)
    assert child["no_effect"] is False and child["revision"] == 2 and p.versions[root]["status"] == "validated"
    v = c.validate_plan_version(child["plan_version_id"], {"plan_version_id": child["plan_version_id"], "idempotency_key": "k-val"}, META)
    assert v["status"] == "invalid" and v["findings"][0]["pointer"] == "/allocation"
    with pytest.raises(ToolError) as e:
        c.publish_plan_version(pl, {"plan_version_id": child["plan_version_id"], "expected_revision": 2, "idempotency_key": "k-pub-1"}, META)
    assert e.value.code == "PRECONDITION_FAILED"
    pub = c.publish_plan_version(pl, {"plan_version_id": root, "expected_revision": 2, "idempotency_key": "k-pub-2"}, META)
    assert validate_document(pub, "publication").valid and c.get_plan(pl, META)["current_publication"]["publication_id"] == pub["publication_id"]
    listing = c.list_plan_versions(pl, META, page_size=1)
    assert len(listing["versions"]) == 1 and listing["next_token"]
    page2 = c.list_plan_versions(pl, META, page_size=1, next_token=listing["next_token"])
    assert page2["versions"][0]["plan_version_id"] != listing["versions"][0]["plan_version_id"]
    ing = c.run_ingestion({"dataset_id": ETF_DATASET, "start_date": "2026-01-02", "end_date": "2026-01-09", "granularity": "daily", "idempotency_key": "k-ing"}, META)
    assert validate_document(ing, "tools/refresh-market-data-response").valid
    assert {call.op for call in p.calls} == set(PLAN_ROUTES)


def test_platform_idempotency_and_conflict(o):
    p = o.platform
    c = PlatformClient(p, p)
    pl, root = p.add_plan(p.add_portfolio())
    body = {"parent_plan_version_id": root, "expected_revision": 1, "idempotency_key": "k1", "domain": "finance", "domain_schema_version": "1.0", "content": p.default_content()}
    a = c.create_plan_version(pl, body, META)
    assert c.create_plan_version(pl, dict(body), META) == a and a["no_effect"] is True
    with pytest.raises(ToolError) as e:
        c.create_plan_version(pl, dict(body, reason="different"), META)
    assert e.value.code == "IDEMPOTENCY_KEY_REUSED"
    with pytest.raises(ToolError) as e:
        c.create_plan_version(pl, dict(body, idempotency_key="k2"), META)
    assert e.value.code == "CONFLICT" and e.value.details["current_revision"] == 2


def test_job_api_routes_conformant(o):
    j = JobClient(o.jobs)
    dry = j.submit_job(research_request(dry_run=True), META)
    assert dry["run_id"] is None and dry["dry_run"] is True and not o.jobs.runs
    run = j.submit_job(research_request(), META)
    assert validate_document(j.get_job_status(run["run_id"], META), "job-status").valid
    with pytest.raises(ToolError) as e:
        j.get_job_result(run["run_id"], META)
    assert e.value.code == "PRECONDITION_FAILED"
    for completion, solution in (("succeeded", "optimal"), ("succeeded", "infeasible"), ("succeeded", "no_effect"), ("failed", None), ("timed_out", None), ("cancelled", None)):
        r = j.submit_job(research_request(idempotency_key=f"k-{completion}-{solution}"), META)
        o.jobs.finish(r["run_id"], completion, solution)
        res = j.get_job_result(r["run_id"], META)
        st = j.get_job_status(r["run_id"], META)
        assert res["completion_status"] == completion == st["completion_status"]
        assert validate_document(res, "job-result").valid and validate_document(st, "job-status").valid
    with pytest.raises(ToolError) as e:
        j.submit_job(research_request(purpose="production_candidate", idempotency_key="k-pc"), META)
    assert e.value.code == "FORBIDDEN"
    o.jobs.remaining_by_category["cpu_research"] = 0.1
    with pytest.raises(ToolError) as e:
        j.submit_job(research_request(job_type="cpu_small", idempotency_key="k-budget"), META)
    assert e.value.code == "BUDGET_EXCEEDED"


def test_gpu_run_waits_for_approval(o):
    run = JobClient(o.jobs).submit_job(research_request(job_type="gpu_training"), META)
    assert run["state"] == "awaiting_approval" and run["cost_estimate"]["budget_category"] == "gpu"
    assert validate_document(JobClient(o.jobs).get_job_status(run["run_id"], META), "job-status").valid


# ---------------------------------------------------------------- scenarios (3.2)
@pytest.mark.parametrize("name", sorted(SCENARIOS))
def test_scenario_loads_and_validates(name):
    o = offline_runtime("beta")
    sc = SCENARIOS[name](o)
    assert sc.name == name and sc.requests
    from finplan_tools.core.registry import CATALOG

    entry = CATALOG[sc.tool]
    for req in sc.requests:
        assert req.get("synthetic") is True, "every scenario request is synthetic"
        res = validate_document(req, entry.input_schema)
        # md_intraday_rejected deliberately asks for an undeclared granularity
        assert res.valid or name == "md_intraday_rejected", (name, [i.message for i in res.issues])
    for schema, doc in sc.documents:
        assert validate_document(doc, schema).valid and doc.get("synthetic") is True
    # market data: the ETF daily dataset only, synthetic observations, mock providers unless allow-listed
    for sid, snap in o.platform.snapshots.items():
        assert snap["dataset"]["dataset_id"] == ETF_DATASET and snap["synthetic"] is True
        assert snap["lineage"]["provider"] in MOCK_PROVIDERS or name in REAL_PROVIDER_LINEAGE_ALLOWED
        assert all(ob["synthetic"] is True and ob["kind"] == "completed_daily" for ob in o.platform.observations[sid])
    text = json.dumps([sc.requests, sc.documents])
    assert "index-level" not in text and "universe/" not in text


def test_required_scenarios_present():
    required = {
        "duplicate", "key_reuse", "conflict", "infeasible", "no_effect", "failed", "timed_out_partial",
        "budget_cpu_over", "budget_gpu_within", "budget_zero_limit_category", "schema_upgrade", "unsupported_major",
        "md_rate_limited", "md_partial_response", "md_committed_snapshot", "md_yfinance_lineage_shape",
    }
    assert required <= set(SCENARIOS)
