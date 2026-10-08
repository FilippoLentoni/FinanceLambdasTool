"""Shared request pipeline (spec tool-request-handling; tasks 2.1-2.9).

The pipeline is exercised with minimal test tool implementations built directly as ``ToolSpec``s
(not registered), wired to the in-process mocks through the real producer clients, so every
assertion about "no downstream call" is a mock call count.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import pytest

from finplan_tools.core.contracts import contract_version, validate_document
from finplan_tools.core.errors import ToolError
from finplan_tools.core.idempotency import DERIVED_KEY_PATTERN
from finplan_tools.core.pipeline import execute
from finplan_tools.core.registry import CATALOG, ToolSpec
from finplan_tools_testing._base import fixture
from finplan_tools_testing.mock_jobs import research_request
from finplan_tools_testing.runtime import offline_runtime

PV_WRONG = "pv_01JA2B3C4D5E6F7G8H9JKMNPQR"


# ---------------------------------------------------------------- test tools
def _get_plan_version(ctx, req):
    return ctx.platform.get_plan_version(req["plan_version_id"], ctx.meta)


def _get_plan(ctx, req):
    return ctx.platform.get_plan(req["plan_id"], ctx.meta)


def _job_status(ctx, req):
    return ctx.jobs.get_job_status(req["run_id"], ctx.meta)


def _submit(ctx, req):
    # FinanceModel validates the contract job-submission envelope (contract_version required)
    body = ctx.downstream_body(req, extra={"contract_version": contract_version()})
    return ctx.jobs.submit_job(body, ctx.meta)


def _override(ctx, req):
    return ctx.platform.create_plan_version(req["plan_id"], ctx.downstream_body(req, drop=("plan_id",)), ctx.meta)


def _publish(ctx, req):
    return ctx.platform.publish_plan_version(req["plan_id"], ctx.downstream_body(req, drop=("plan_id",)), ctx.meta)


def _list_versions(ctx, req):
    return ctx.platform.list_plan_versions(req["plan_id"], ctx.meta, page_size=req.get("page_size"))


def spec(name: str, run, **kw: Any) -> ToolSpec:
    return ToolSpec(CATALOG[name], f"test {name}", run, **kw)


GET_PV = spec("get_plan_version", _get_plan_version)
GET_PLAN = spec("get_plan", _get_plan)
JOB_STATUS = spec("get_job_status", _job_status)
SUBMIT = spec("submit_experiment", _submit)
OVERRIDE = spec("create_override_version", _override)
PUBLISH = spec("publish_plan_version", _publish)
LIST = spec("list_plan_versions", _list_versions, list_key="versions", truncated_key=None)


def run(o, s: ToolSpec, args: Any, **meta: Any) -> dict[str, Any]:
    return execute(s, o.event(args, **meta), None, o.runtime)


def is_error(resp: dict[str, Any], code: str | None = None) -> bool:
    ok = {"code", "message", "retryable", "correlation_id", "contract_version"} <= set(resp)
    return ok and (code is None or resp["code"] == code)


@pytest.fixture
def world():
    o = offline_runtime("beta")
    pf = o.platform.add_portfolio()
    pl, pv = o.platform.add_plan(pf)
    o.platform.calls.clear()
    return o, {"portfolio_id": pf, "plan_id": pl, "plan_version_id": pv}


# ================================================================ TRH-04 identity
def test_unknown_source_is_unauthorized_without_calls(world):
    o, ids = world
    resp = execute(GET_PV, {"arguments": {"plan_version_id": ids["plan_version_id"]}}, None, o.runtime)
    assert is_error(resp, "UNAUTHORIZED") and o.platform.count() == 0
    resp = execute(GET_PV, {"finplan_invocation": {"source": "admin"}, "arguments": {}}, None, o.runtime)
    assert is_error(resp, "UNAUTHORIZED")


def test_body_caller_is_ignored_and_logged_untrusted(world, caplog):
    o, ids = world
    caplog.set_level(logging.INFO, logger="finplan_tools.audit")
    resp = run(o, GET_PV, {"plan_version_id": ids["plan_version_id"], "caller": "admin"}, caller="admin")
    assert not is_error(resp), resp
    rec = json.loads(caplog.records[-1].getMessage())
    assert rec["caller_identity"] == "direct:beta" and rec["channel"] == "direct_test"
    assert rec["untrusted"] == {"caller": "admin"}
    caller_hdr = json.loads(o.platform.calls[-1].headers["X-Finplan-Caller"])
    assert caller_hdr["channel"] == "direct_test" and "admin" not in json.dumps(caller_hdr)


def test_gateway_context_identity(world):
    o, ids = world

    class Ctx:
        class client_context:
            custom = {"bedrockAgentCoreToolName": "tools___get_plan_version"}

    resp = execute(GET_PV, {"plan_version_id": ids["plan_version_id"]}, Ctx(), o.runtime)
    assert resp["plan_version"]["plan_version_id"] == ids["plan_version_id"]
    assert json.loads(o.platform.calls[-1].headers["X-Finplan-Caller"])["channel"] == "hosted_agent"


def test_misrouted_tool_name_rejected(world):
    o, ids = world
    resp = run(o, GET_PV, {"plan_version_id": ids["plan_version_id"]}, tool="publish_plan_version")
    assert is_error(resp, "VALIDATION_FAILED") and o.platform.count() == 0


# ================================================================ TRH-01 validation
def test_missing_required_parameter(world):
    o, _ = world
    resp = run(o, GET_PV, {})
    assert is_error(resp, "VALIDATION_FAILED") and resp["details"]["pointer"] == "/plan_version_id"
    assert resp["retryable"] is False and o.platform.count() == 0


def test_success_response_conforms(world):
    o, ids = world
    resp = run(o, GET_PV, {"plan_version_id": ids["plan_version_id"]})
    assert validate_document(resp, "tools/get-plan-version-response").valid


def test_non_conformant_response_is_never_sent(world):
    o, ids = world
    bad = spec("get_plan_version", lambda ctx, req: {"plan_version": {"plan_version_id": "nope"}})
    resp = run(o, bad, {"plan_version_id": ids["plan_version_id"]})
    assert is_error(resp, "INTERNAL") and "nope" not in json.dumps(resp)


# ================================================================ TRH-02 contract major
def test_unsupported_major(world):
    o, ids = world
    resp = run(o, GET_PV, {"plan_version_id": ids["plan_version_id"], "contract_version": "2.0.0"})
    assert is_error(resp, "UNSUPPORTED_CONTRACT_VERSION")
    assert resp["details"]["served_contract_majors"] == [1] and resp["details"]["served_majors"] == [1]
    assert o.platform.count() == 0
    assert validate_document(resp, "error").valid


@pytest.mark.parametrize("declared", ["1.0.0", "1.4.2"])
def test_same_major_accepted(world, declared):
    o, ids = world
    resp = run(o, GET_PV, {"plan_version_id": ids["plan_version_id"], "contract_version": declared})
    assert not is_error(resp), resp


def test_major_in_invocation_envelope(world):
    o, ids = world
    resp = run(o, GET_PV, {"plan_version_id": ids["plan_version_id"]}, contract_version="2.1.0")
    assert is_error(resp, "UNSUPPORTED_CONTRACT_VERSION")


# ================================================================ TRH-03 identifiers
def test_wrong_prefix_is_invalid_identifier_without_model_call(world):
    o, _ = world
    resp = run(o, JOB_STATUS, {"run_id": PV_WRONG})
    assert is_error(resp, "INVALID_IDENTIFIER") and resp["details"]["field"] == "run_id"
    assert o.jobs.count() == 0


def test_caller_supplied_minted_identifier_rejected(world):
    o, ids = world
    req = fixture("tools/create-override-version-request", "override")
    req.update(plan_id=ids["plan_id"], parent_plan_version_id=ids["plan_version_id"], plan_version_id="pv_01KM0000000000000000000099")
    resp = run(o, OVERRIDE, req)
    assert is_error(resp, "VALIDATION_FAILED") and o.platform.count() == 0


# ================================================================ TRH-09 storage inputs
@pytest.mark.parametrize("value", ["s3://example-bucket/out/", "arn:aws:s3:::example-bucket", "/tmp/out.json", "../x/y"])
def test_storage_locations_rejected_before_any_call(world, value):
    o, _ = world
    req = research_request(input_snapshot_id=value)
    resp = run(o, SUBMIT, req)
    assert is_error(resp, "VALIDATION_FAILED") and o.jobs.count() == 0 and o.platform.count() == 0
    assert "example-bucket" not in json.dumps(resp)


def test_storage_field_name_rejected(world):
    o, _ = world
    req = research_request()
    req["configuration"]["payload"]["output_location"] = "results"
    resp = run(o, SUBMIT, req)
    assert is_error(resp, "VALIDATION_FAILED") and resp["details"]["pointer"] == "/configuration/payload/output_location"


# ================================================================ TRH-05 environment
def test_gamma_request_to_beta_lambda_forbidden(world):
    o, ids = world
    resp = run(o, GET_PV, {"plan_version_id": ids["plan_version_id"]}, environment="gamma")
    assert is_error(resp, "FORBIDDEN") and o.platform.count() == 0


def test_prod_direct_write_forbidden():
    o = offline_runtime("prod")
    resp = run(o, SUBMIT, research_request())
    assert is_error(resp, "FORBIDDEN") and o.jobs.count() == 0
    resp = run(o, SUBMIT, research_request(), source="ci_test")
    assert is_error(resp, "FORBIDDEN")


# ================================================================ dependency gating
def test_model_absent_is_dependency_unavailable_without_calls():
    o = offline_runtime("beta", with_model=False)
    resp = run(o, JOB_STATUS, {"run_id": "run_01KM0000000000000000000001"})
    # EXP-02: retryable false (the producer is not released here; a retry cannot change that)
    assert is_error(resp, "DEPENDENCY_UNAVAILABLE") and resp["retryable"] is False and o.jobs.count() == 0


def test_platform_serving_other_major():
    o = offline_runtime("beta", platform_majors=[2])
    resp = run(o, GET_PLAN, {"plan_id": "pl_01KM0000000000000000000001"})
    assert is_error(resp, "UNSUPPORTED_CONTRACT_VERSION") and o.platform.count() == 0


def test_model_release_appears_within_one_ttl():
    o = offline_runtime("beta", with_model=False)
    t = {"now": 0.0}
    o.runtime.references._clock = lambda: t["now"]  # noqa: SLF001
    o.runtime.references.invalidate()
    assert is_error(run(o, JOB_STATUS, {"run_id": "run_01KM0000000000000000000001"}), "DEPENDENCY_UNAVAILABLE")
    from finplan_tools_testing.runtime import release_manifest

    o.params.put("/finplan/beta/financemodel/release/manifest", release_manifest("financemodel", "beta"))
    assert is_error(run(o, JOB_STATUS, {"run_id": "run_01KM0000000000000000000001"}), "DEPENDENCY_UNAVAILABLE")  # cached absence
    t["now"] = 301.0
    assert is_error(run(o, JOB_STATUS, {"run_id": "run_01KM0000000000000000000001"}), "NOT_FOUND")  # reached the producer


# ================================================================ TRH-06 idempotency
def test_duplicate_submit_gives_one_run(world):
    o, _ = world
    req = research_request()
    a, b = run(o, SUBMIT, req), run(o, SUBMIT, dict(req))
    assert a["run_id"] == b["run_id"] and len(o.jobs.runs) == 1
    sent = [c.body for c in o.jobs.calls if c.op == "submit_job"]
    assert sent[0] == sent[1] and DERIVED_KEY_PATTERN.match(sent[0]["idempotency_key"])
    assert sent[0]["idempotency_key"] != req["idempotency_key"]
    assert sent[0]["contract_version"] == contract_version()  # pinned version, never the caller's (job-submission requires it)


def test_key_reused_with_different_body(world):
    o, _ = world
    req = research_request()
    run(o, SUBMIT, req)
    resp = run(o, SUBMIT, dict(req, evaluation_window={"start": "2026-01-05", "end": "2026-01-09"}))
    assert is_error(resp, "IDEMPOTENCY_KEY_REUSED") and resp["retryable"] is False


def test_two_callers_same_key_do_not_collide(world):
    o, ids = world
    req = fixture("tools/create-override-version-request", "override")
    req.update(plan_id=ids["plan_id"], parent_plan_version_id=ids["plan_version_id"], expected_revision=1, idempotency_key="k1")
    first = run(o, OVERRIDE, req, source="direct_test")
    assert not is_error(first), first
    req2 = dict(req, expected_revision=first["revision"])
    second = run(o, OVERRIDE, req2, source="ci_test")
    assert not is_error(second), second
    keys = [c.body["idempotency_key"] for c in o.platform.calls if c.op == "create_plan_version"]
    assert keys[0] != keys[1] and first["plan_version_id"] != second["plan_version_id"]


def test_write_tool_without_key(world):
    o, ids = world
    req = fixture("tools/publish-plan-version-request", "publish")
    req.pop("idempotency_key")
    resp = run(o, PUBLISH, req)
    assert is_error(resp, "VALIDATION_FAILED") and o.platform.count() == 0


# ================================================================ TRH-07/08 errors
def test_conflict_passed_through_with_correlation_in_logs(world, caplog):
    o, ids = world
    caplog.set_level(logging.INFO)
    req = fixture("tools/create-override-version-request", "override")
    req.update(plan_id=ids["plan_id"], parent_plan_version_id=ids["plan_version_id"], expected_revision=7)
    resp = run(o, OVERRIDE, req, correlation_id="corr-test-conflict-0001")
    assert is_error(resp, "CONFLICT") and resp["retryable"] is False
    assert resp["correlation_id"] == "corr-test-conflict-0001"
    assert any("corr-test-conflict-0001" in r.getMessage() for r in caplog.records)
    assert o.platform.calls[-1].headers["X-Correlation-Id"] == "corr-test-conflict-0001"


def test_unhandled_exception_is_internal_without_trace(world, caplog):
    o, ids = world

    def boom(ctx, req):
        raise RuntimeError("secret-ish detail s3://example-bucket/key")

    caplog.set_level(logging.INFO)
    resp = run(o, spec("get_plan_version", boom), {"plan_version_id": ids["plan_version_id"]})
    assert is_error(resp, "INTERNAL")
    text = json.dumps(resp)
    assert "Traceback" not in text and "example-bucket" not in text and "secret-ish" not in text
    traced = [r for r in caplog.records if r.exc_info and resp["correlation_id"] in r.getMessage()]
    assert traced, "the full trace must be logged under the same correlation_id"


def _registered_codes() -> list[str]:
    from finplan_tools.core.contracts import registered_error_codes

    return sorted(registered_error_codes())


@pytest.mark.parametrize("code", _registered_codes())
def test_every_registered_producer_code_passes_through(world, code):
    o, ids = world
    details = {"pointer": "/x"} if code == "VALIDATION_FAILED" else {"field": "x"} if code == "INVALID_IDENTIFIER" else {"served_contract_majors": [1]} if code == "UNSUPPORTED_CONTRACT_VERSION" else {}
    o.platform.fail_next("get_plan_version", code, details=details)
    resp = run(o, GET_PV, {"plan_version_id": ids["plan_version_id"]})
    from finplan_tools.core.errors import default_retryable

    assert is_error(resp, code) and resp["retryable"] == default_retryable(code)
    assert validate_document(resp, "error").valid


def test_retryable_flag_of_producer_kept_when_not_fixed(world):
    o, ids = world
    o.platform.fail_next("get_plan_version", "DEPENDENCY_UNAVAILABLE", retryable=False)
    resp = run(o, GET_PV, {"plan_version_id": ids["plan_version_id"]})
    assert is_error(resp, "DEPENDENCY_UNAVAILABLE") and resp["retryable"] is False


@pytest.mark.parametrize("status,code", [(429, "RATE_LIMITED"), (503, "DEPENDENCY_UNAVAILABLE"), (500, "INTERNAL"), (403, "FORBIDDEN")])
def test_non_envelope_producer_errors(world, status, code):
    o, ids = world
    o.platform.fail_next("get_plan_version", "INTERNAL", status=status, raw_body={"message": "Missing Authentication Token"})
    assert is_error(run(o, GET_PV, {"plan_version_id": ids["plan_version_id"]}), code)


def test_unreachable_producer_is_retryable_dependency_unavailable(world):
    o, ids = world
    o.platform.unreachable = True
    resp = run(o, GET_PV, {"plan_version_id": ids["plan_version_id"]})
    assert is_error(resp, "DEPENDENCY_UNAVAILABLE") and resp["retryable"] is True


def test_missing_route_is_dependency_unavailable(world):
    o, ids = world
    o.platform.missing_routes.add("list_plan_versions")
    resp = run(o, LIST, {"plan_id": ids["plan_id"]})
    assert is_error(resp, "DEPENDENCY_UNAVAILABLE")


def test_throttled_publish_retry_publishes_once(world):
    o, ids = world
    # validate the root version is already "validated" (seeded); publish at revision 1
    req = {"plan_id": ids["plan_id"], "plan_version_id": ids["plan_version_id"], "expected_revision": 1, "idempotency_key": "pub-1", "synthetic": True}
    o.platform.fail_next("publish_plan_version", "RATE_LIMITED")
    first = run(o, PUBLISH, req)
    assert is_error(first, "RATE_LIMITED") and first["retryable"] is True
    second, third = run(o, PUBLISH, req), run(o, PUBLISH, req)
    assert second["publication_id"] == third["publication_id"] and len(o.platform.publications) == 1


# ================================================================ TRH-10 bounds
def test_large_list_is_truncated_with_token(world):
    o, ids = world
    p = o.platform
    for _ in range(60):
        p._new_version(ids["plan_id"], ids["plan_version_id"], p.default_content(), origin="manual_override", status="pending_validation")  # noqa: SLF001
    limits = {"response_max_bytes": 4096}
    o2 = o
    o2.set_param("financelambdastool", "config", "tool-limits", limits)
    resp = run(o2, LIST, {"plan_id": ids["plan_id"], "page_size": 50})
    assert not is_error(resp), resp
    assert len(json.dumps(resp, separators=(",", ":"), sort_keys=True).encode()) <= 4096
    assert resp["next_token"] and len(resp["versions"]) < 50
    from finplan_tools.core.bounds import unwrap_token

    tok = unwrap_token(resp["next_token"], tool="list_plan_versions", environment="beta")
    assert tok["offset"] == len(resp["versions"])
    with pytest.raises(ToolError):
        unwrap_token(resp["next_token"], tool="list_plan_versions", environment="gamma")


# ================================================================ TRH-11 no mutable caching
def test_head_change_visible_on_next_call(world):
    o, ids = world
    a = run(o, GET_PLAN, {"plan_id": ids["plan_id"]})
    o.platform.plans[ids["plan_id"]]["head"] = {"current_version_id": ids["plan_version_id"], "revision": 9}
    b = run(o, GET_PLAN, {"plan_id": ids["plan_id"]})
    assert a["plan"]["head"]["revision"] == 1 and b["plan"]["head"]["revision"] == 9
    assert o.platform.count("get_plan") == 2


# ================================================================ TRH-09 response leak scan
def test_response_with_storage_location_suppressed(world):
    o, ids = world

    def leaky(ctx, req):
        doc = ctx.platform.get_plan_version(req["plan_version_id"], ctx.meta)
        doc["content_summary"] = {"note": "s3://example-bucket/plans/x.json"}
        return doc

    resp = run(o, spec("get_plan_version", leaky), {"plan_version_id": ids["plan_version_id"]})
    assert is_error(resp, "INTERNAL") and "example-bucket" not in json.dumps(resp)


# ================================================================ TRH-12 audit
def test_one_audit_record_with_required_fields_and_no_payload(world, caplog):
    o, ids = world
    caplog.set_level(logging.INFO, logger="finplan_tools.audit")
    req = {"plan_id": ids["plan_id"], "plan_version_id": ids["plan_version_id"], "expected_revision": 1, "idempotency_key": "audit-key-1", "synthetic": True}
    resp = run(o, PUBLISH, req)
    recs = [json.loads(r.getMessage()) for r in caplog.records if r.name == "finplan_tools.audit"]
    assert len(recs) == 1
    rec = recs[0]
    for k in ("correlation_id", "tool", "caller_identity", "environment", "contract_version", "release_id", "outcome", "downstream_ids"):
        assert k in rec
    assert rec["outcome"] == "OK" and rec["contract_version"] == contract_version()
    assert rec["downstream_ids"]["publication_id"] == [resp["publication_id"]]
    assert rec["downstream_ids"]["plan_version_id"] == [ids["plan_version_id"]]
    text = json.dumps(rec)
    assert "audit-key-1" not in text and "lt_" not in text and "expected_revision" not in text and "https://" not in text


def test_audit_record_on_error(world, caplog):
    o, _ = world
    caplog.set_level(logging.INFO, logger="finplan_tools.audit")
    run(o, GET_PV, {})
    recs = [json.loads(r.getMessage()) for r in caplog.records if r.name == "finplan_tools.audit"]
    assert len(recs) == 1 and recs[0]["outcome"] == "VALIDATION_FAILED" and recs[0]["retryable"] is False
