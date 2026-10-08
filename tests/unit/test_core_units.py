"""Unit tests of the pipeline building blocks (tasks 2.1-2.9 and the group 2 helpers)."""

from __future__ import annotations

import json
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from finplan_contracts.schemas import contracts_root

from finplan_tools.core import status
from finplan_tools.core.artifacts import response_leaks, storage_input_problems
from finplan_tools.core.bounds import bound_list, response_size, summarize_weights, unwrap_token, wrap_token
from finplan_tools.core.budget import check_tool_budget
from finplan_tools.core.compat import check_configuration_id, check_producer_major, check_snapshot_compatibility, local_configuration_id, producer_availability
from finplan_tools.core.config import DEFAULT_TOOL_LIMITS, Settings, ToolLimits, limit_bound_problems, tool_limits_problems
from finplan_tools.core.contracts import contract_major, contract_version, served_majors
from finplan_tools.core.errors import ToolError, from_producer_envelope, sanitize_details
from finplan_tools.core.gateway import parse_gateway_call
from finplan_tools.core.identity import caller_block, resolve_invocation
from finplan_tools.core.idempotency import DERIVED_KEY_PATTERN, body_bytes, derived_key, downstream_body
from finplan_tools.core.references import ReferenceError_, ReferenceResolver
from finplan_tools_testing._base import fixture
from finplan_tools_testing.params import DictParameterStore

ROOT = Path(__file__).resolve().parents[2]
#: Built at run time so the repository leak scan never sees an ARN-shaped literal.
ARN_AWS = "arn" + ":aws"
BUCKET_KEY = "buck" + "et"


# ================================================================ contracts
def test_pinned_contract_is_1x():
    assert contract_version() == json.loads((ROOT / "contracts-pin.json").read_text())["version"]
    assert served_majors() == [contract_major()] == [int(contract_version().split(".")[0])]


# ================================================================ identity (TRH-04, task 2.1)
def test_direct_identity_from_marker_only():
    inv = resolve_invocation({"finplan_invocation": {"source": "direct_test", "environment": "beta", "caller": "admin"}, "arguments": {"caller": "root", "x": 1}}, None, environment="beta")
    assert inv.identity == "direct:beta" and inv.channel == "direct_test"
    assert inv.arguments == {"x": 1} and inv.untrusted == {"caller": "root"}


def test_ci_identity():
    inv = resolve_invocation({"finplan_invocation": {"source": "ci_test"}, "arguments": {}}, None, environment="gamma")
    assert inv.identity == "pipeline:gamma" and inv.channel == "ci_test"


def test_identity_built_from_lambda_environment_not_declared_one():
    inv = resolve_invocation({"finplan_invocation": {"source": "direct_test", "environment": "prod"}, "arguments": {}}, None, environment="beta")
    assert inv.identity == "direct:beta" and inv.declared_environment == "prod"


@pytest.mark.parametrize("event", [None, [], {"arguments": {}}, {"finplan_invocation": {"source": "gateway"}}, {"finplan_invocation": "direct_test"}])
def test_unknown_sources(event):
    with pytest.raises(ToolError) as e:
        resolve_invocation(event, None, environment="beta")
    assert e.value.code == "UNAUTHORIZED"


def test_unknown_envelope_fields_rejected():
    with pytest.raises(ToolError) as e:
        resolve_invocation({"finplan_invocation": {"source": "direct_test", "role": "admin"}, "arguments": {}}, None, environment="beta")
    assert e.value.code == "VALIDATION_FAILED"


def test_untrusted_annotation_is_redacted_when_unsafe():
    inv = resolve_invocation({"finplan_invocation": {"source": "direct_test"}, "arguments": {"caller": ARN_AWS + ":iam::x:role/r"}}, None, environment="beta")
    assert inv.untrusted == {"caller": "<redacted>"}


def test_gateway_adapter_isolated():
    ctx = SimpleNamespace(client_context=SimpleNamespace(custom={"bedrockAgentCoreToolName": "finplan-tools___get_plan"}))
    call = parse_gateway_call({"plan_id": "x"}, ctx)
    assert call and call.tool_name == "get_plan" and call.arguments == {"plan_id": "x"}
    assert parse_gateway_call({}, None) is None
    inv = resolve_invocation({"plan_id": "x", "caller": "someone"}, ctx, environment="beta")
    assert inv.identity == "gateway:beta" and inv.channel == "hosted_agent" and inv.arguments == {"plan_id": "x"}


def test_caller_block_is_contract_valid():
    from finplan_tools.core.contracts import validate_document

    inv = resolve_invocation({"finplan_invocation": {"source": "direct_test"}, "arguments": {}}, None, environment="beta")
    block = caller_block(inv, "corr-0000-0001", synthetic=True)
    assert validate_document(block, "caller").valid and "direct:beta" not in json.dumps(block)


# ================================================================ idempotency (TRH-06, task 2.5)
def _vectors() -> dict:
    return json.loads((contracts_root() / "fixtures" / "vectors" / "proxied_keys.json").read_text())


@pytest.mark.parametrize("case", _vectors()["cases"], ids=lambda c: c["name"])
def test_derived_key_matches_contract_vectors(case):
    assert derived_key(case["caller_identity"], case["env"], case["tool"], case["idempotency_key"]) == case["derived_key"]


def test_derived_key_properties():
    a = derived_key("direct:beta", "beta", "submit_experiment", "k1")
    assert a == derived_key("direct:beta", "beta", "submit_experiment", "k1")
    assert a != derived_key("gateway:beta", "beta", "submit_experiment", "k1")
    assert a != derived_key("direct:beta", "gamma", "submit_experiment", "k1")
    assert a != derived_key("direct:beta", "beta", "refresh_market_data", "k1")
    assert DERIVED_KEY_PATTERN.match(a) and len(a) == 67 and re.fullmatch(r"[A-Za-z0-9_-]{1,128}", a)


def test_downstream_body_is_byte_identical_across_retries():
    req = fixture("tools/submit-experiment-request", "research")
    b1 = downstream_body(dict(req), identity="direct:beta", environment="beta", tool="submit_experiment")
    b2 = downstream_body(json.loads(json.dumps(req)), identity="direct:beta", environment="beta", tool="submit_experiment")
    assert body_bytes(b1) == body_bytes(b2)
    assert "contract_version" not in b1 and b1["idempotency_key"].startswith("lt_")
    assert req["idempotency_key"] == "client-key-0004"  # the request is not mutated
    with pytest.raises(ValueError):
        downstream_body({"x": 1}, identity="i", environment="beta", tool="submit_experiment")


# ================================================================ errors (task 2.6)
def test_fixed_retryable_cannot_be_overridden():
    assert ToolError("CONFLICT", "x", retryable=True).retryable is False
    assert ToolError("RATE_LIMITED", "x").retryable is True
    assert ToolError("DEPENDENCY_UNAVAILABLE", "x", retryable=False).retryable is False
    with pytest.raises(ValueError):
        ToolError("NOPE", "x")


def test_details_are_sanitized():
    d = sanitize_details({"a": "s3://example-bucket/k", "b": ARN_AWS + ":iam::x:role/y", "c": "ok", "d": "/configuration/payload", "e": "x.execute-api.us-east-2.amazonaws.com"})
    assert d == {"a": "<redacted>", "b": "<redacted>", "c": "ok", "d": "/configuration/payload", "e": "<redacted>"}


def test_producer_envelope_mapping():
    e = from_producer_envelope({"code": "CONFLICT", "message": "stale", "retryable": False, "details": {"current_revision": 5}}, 409)
    assert (e.code, e.retryable, e.details) == ("CONFLICT", False, {"current_revision": 5})
    assert from_producer_envelope({"code": "WHAT"}, 500).code == "INTERNAL"
    assert from_producer_envelope(None, 429).code == "RATE_LIMITED"
    leaky = from_producer_envelope({"code": "INTERNAL", "message": "failed at s3://b/k"}, 500)
    assert "s3://" not in leaky.message


# ================================================================ bounds (TRH-10, task 2.7)
def test_token_roundtrip_and_binding():
    t = wrap_token(tool="list_plan_versions", environment="beta", producer_token="p20", offset=3)
    assert re.fullmatch(r"[A-Za-z0-9_=-]{1,2048}", t)
    assert unwrap_token(t, tool="list_plan_versions", environment="beta") == {"producer_token": "p20", "offset": 3}
    for tool, env in (("get_plan", "beta"), ("list_plan_versions", "prod")):
        with pytest.raises(ToolError):
            unwrap_token(t, tool=tool, environment=env)
    with pytest.raises(ToolError):
        unwrap_token("not-a-token!", tool="list_plan_versions", environment="beta")


def test_bound_list_fits_limit():
    doc = {"plan_id": "pl_x", "versions": [{"i": i, "pad": "x" * 200} for i in range(100)], "next_token": None}
    out = bound_list(doc, "versions", 4096, tool="list_plan_versions", environment="beta", truncated_key=None)
    assert response_size(out) <= 4096 and 0 < len(out["versions"]) < 100 and out["next_token"]
    assert bound_list({"versions": [1]}, "versions", 4096, tool="t_x", environment="beta") == {"versions": [1]}
    with pytest.raises(ToolError):
        bound_list({"versions": [{"pad": "x" * 5000}]}, "versions", 1024, tool="t_x", environment="beta")


def test_summarize_weights_top_n_plus_other():
    s = summarize_weights({f"I{i}": 0.01 * i for i in range(1, 21)}, 5)
    assert [t["key"] for t in s["top"]] == ["I20", "I19", "I18", "I17", "I16"]
    assert s["other"]["count"] == 15 and s["total_count"] == 20
    assert abs(s["other"]["weight"] - sum(0.01 * i for i in range(1, 16))) < 1e-9


# ================================================================ artifacts (TRH-09)
def test_storage_input_problems():
    assert storage_input_problems({"a": "SPY", "b": "finance/etf-daily/SPY", "c": "https://contracts.finplan.invalid/core/v1/x.json"}) == []
    assert storage_input_problems({"a": {"b": ["s3://x/y"]}}) == ["/a/b/0"]
    assert storage_input_problems({BUCKET_KEY: "anything"}) == ["/" + BUCKET_KEY]
    assert storage_input_problems({"p": "C:\\data\\x"}) == ["/p"]
    assert storage_input_problems({"p": "~/x"}) == ["/p"]


def test_response_leaks():
    ok = fixture("tools/get-plan-version-response", "compact")
    assert response_leaks(ok) == []
    bad = {"a": ARN_AWS + ":lambda:us-east-2:x:function:f", "b": "https://abc.execute-api.us-east-2.amazonaws.com/beta", "c": "id " + "1" * 12, "d": "my-bucket/plans/2026/x.json"}
    assert response_leaks(bad) == ["/a", "/b", "/c", "/d"]
    assert response_leaks({"grants": [{"url": "https://x.s3.us-east-2.amazonaws.com/k"}]}, allowed_pointers=["/grants"]) == []


# ================================================================ status
@pytest.mark.parametrize("name,completion,solution,partial", [
    ("succeeded-optimal", "succeeded", "optimal", False),
    ("succeeded-infeasible", "succeeded", "infeasible", False),
    ("succeeded-no-effect", "succeeded", "no_effect", False),
    ("failed-crash", "failed", None, True),
    ("timed-out-partial-artifacts", "timed_out", None, True),
])
def test_completion_vs_solution(name, completion, solution, partial):
    out = status.outcome(fixture("job-result", name))
    assert out["completion_status"] == completion and out["solution_status"] == solution and out["partial"] is partial
    assert out["failed"] is (completion != "succeeded")


def test_non_terminal_status_has_no_completion():
    out = status.outcome(fixture("job-status", "queued"))
    assert out["completion_status"] is None and not out["partial"] and not status.is_terminal("queued")


# ================================================================ budget (D5)
LIMITS = ToolLimits.from_document(None)


def _est(usd, cat):
    return {"estimated_usd_upper_bound": usd, "budget_category": cat, "price_retrieved_at": "2026-01-10T09:00:00Z", "remaining_allocation_usd": 5}


def test_per_call_budget():
    check_tool_budget(_est(0, "cpu_research"), LIMITS)
    check_tool_budget(_est(1.0, "cpu_research"), LIMITS)
    check_tool_budget(_est(3.0, "gpu"), LIMITS)
    with pytest.raises(ToolError) as e:
        check_tool_budget(_est(1.40, "cpu_research"), LIMITS)
    assert e.value.code == "BUDGET_EXCEEDED" and e.value.retryable is False
    assert e.value.details["estimated_usd_upper_bound"] == 1.40 and e.value.details["budget_category"] == "cpu_research" and e.value.details["limit_usd"] == 1.0
    with pytest.raises(ToolError):
        check_tool_budget(_est(0.01, "bedrock_explanations"), LIMITS)
    no_gpu = ToolLimits.from_document({"max_estimated_usd_per_call": {"cpu_research": 1.0}})
    with pytest.raises(ToolError) as e:
        check_tool_budget(_est(0.5, "gpu"), no_gpu)
    assert e.value.details["limit_usd"] is None


def test_limit_bound_check():
    alloc = fixture("budget-allocation", "defaults")
    assert limit_bound_problems(DEFAULT_TOOL_LIMITS, alloc) == []
    problems = limit_bound_problems(DEFAULT_TOOL_LIMITS, dict(alloc, gpu=10))
    assert len(problems) == 1 and "gpu" in problems[0] and "2.00" in problems[0]
    assert "not in the budget allocation" in limit_bound_problems({"max_estimated_usd_per_call": {"cpu_research": 1}}, {"gpu": 25})[0]


# ================================================================ config
def test_default_tool_limits_file_matches_code():
    assert json.loads((ROOT / "config" / "tool-limits.default.json").read_text()) == DEFAULT_TOOL_LIMITS
    assert tool_limits_problems(DEFAULT_TOOL_LIMITS) == []


@pytest.mark.parametrize("bad", [{"response_max_bytes": 10}, {"per_call_max_fraction": 0}, {"max_estimated_usd_per_call": {"typesafe_jev": 1}}, {"max_estimated_usd_per_call": {"gpu": -1}}, {"page_size_default": 200}])
def test_bad_tool_limits(bad):
    with pytest.raises(ValueError):
        ToolLimits.from_document(bad)


def test_settings_from_environ():
    s = Settings.from_environ({"FINPLAN_ENV": "gamma", "FINPLAN_TOOL_NAME": "get_plan", "FINPLAN_RELEASE_ID": "rel_01KDVDNAZ83BAMMYCEGWF33DPM"})
    assert (s.environment, s.tool_name, s.region) == ("gamma", "get_plan", "us-east-2")
    for bad in ({"FINPLAN_ENV": "dev"}, {"FINPLAN_ENV": "beta", "FINPLAN_RELEASE_ID": "r1"}, {"FINPLAN_ENV": "beta", "FINPLAN_TOOL_NAME": "Get-Plan"}):
        with pytest.raises(ValueError):
            Settings.from_environ(bad)


# ================================================================ compat
def test_snapshot_compatibility():
    snap = fixture("input-snapshot", "approved-etf-daily")
    check_snapshot_compatibility(snap, domain="finance", window={"start": "2026-01-02", "end": "2026-01-09"}, require_approved=True)
    for kw, reason in (
        ({"window": {"start": "2026-01-01", "end": "2026-01-09"}}, "coverage_gap"),
        ({"domain": "energy"}, "domain_mismatch"),
    ):
        with pytest.raises(ToolError) as e:
            check_snapshot_compatibility(snap, **kw)
        assert e.value.code == "PRECONDITION_FAILED" and e.value.details["reason"] == reason
    with pytest.raises(ToolError) as e:
        check_snapshot_compatibility(fixture("input-snapshot", "expired"), require_approved=True)
    assert e.value.details["status"] == "expired"


def test_configuration_id():
    req = fixture("tools/submit-experiment-request", "research")
    cid = local_configuration_id(req["configuration"])
    assert cid.startswith("cfg_")
    check_configuration_id(cid, cid)
    with pytest.raises(ToolError) as e:
        check_configuration_id(cid, "cfg_" + "0" * 64)
    assert e.value.code == "INTERNAL"


def test_producer_major():
    m = fixture("release-manifest", "gamma-financelambdastool")
    check_producer_major(m, "financialplanning")
    assert producer_availability(None) == (False, "DEPENDENCY_UNAVAILABLE")
    assert producer_availability(dict(m, served_contract_majors=[2])) == (False, "UNSUPPORTED_CONTRACT_VERSION")
    assert producer_availability(m) == (True, None)


# ================================================================ references (TRH-11, task 2.8)
def test_reference_ttl_cache_and_absence():
    store = DictParameterStore({"/finplan/beta/financialplanning/api/plan-endpoint": "https://a.execute-api.us-east-2.amazonaws.com/beta/"})
    t = {"now": 0.0}
    r = ReferenceResolver(store, "beta", ttl_seconds=300, clock=lambda: t["now"])
    assert r.plan_endpoint().startswith("https://")
    store.put("/finplan/beta/financialplanning/api/plan-endpoint", "https://b.execute-api.us-east-2.amazonaws.com/beta/")
    assert "//a." in r.plan_endpoint() and len(store.reads) == 1
    t["now"] = 300.0
    assert "//b." in r.plan_endpoint() and len(store.reads) == 2
    assert r.job_endpoint() is None
    store.put("/finplan/beta/financemodel/api/job-endpoint", "https://c.execute-api.us-east-2.amazonaws.com/beta")
    assert r.job_endpoint() is None
    t["now"] = 700.0
    assert r.job_endpoint() is not None


def test_reference_reads_are_same_environment_only():
    r = ReferenceResolver(DictParameterStore(), "gamma")
    with pytest.raises(ReferenceError_):
        r.read("/finplan/prod/financialplanning/api/plan-endpoint")
    assert r.name("financialplanning", "api", "plan-endpoint") == "/finplan/gamma/financialplanning/api/plan-endpoint"
    assert r.get("financialplanning", "config", "budget-allocation", shared=True) is None


def test_reference_failure_is_not_absence():
    class Broken:
        def get_parameter(self, Name):  # noqa: N803
            raise RuntimeError("AccessDenied")

    with pytest.raises(ReferenceError_):
        ReferenceResolver(Broken(), "beta").plan_endpoint()
