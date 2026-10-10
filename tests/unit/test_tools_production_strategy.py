"""``production_strategy`` tool (change add-approval-and-strategy-tools, tasks 1.1-1.4; PST-01..PST-04)
against the in-process FinanceModel mock. Every negative case asserts zero FinanceModel calls."""

from __future__ import annotations

import json
import re
from pathlib import Path
from types import SimpleNamespace

import pytest
from finplan_contracts import boundaries as contract_boundaries
from finplan_contracts.iam import Request, evaluate

from finplan_tools.core.contracts import contract_version, schema_id, validate_document
from finplan_tools.core.idempotency import DERIVED_KEY_PATTERN
from finplan_tools.core.pipeline import execute
from finplan_tools.core.registry import CATALOG, get_tool
from finplan_tools.tools import load_all
from finplan_tools_testing.runtime import offline_runtime, release_manifest
from infra.stacks.policies import ProducerApis, role_class_policy
from tests.unit.infra_support import ACCOUNT, PARTITION, REGION

TOOL = "production_strategy"
ROOT = Path(__file__).resolve().parents[2]
KW = {"partition": PARTITION, "region": REGION, "account": ACCOUNT}


def is_error(resp, code=None):
    return {"code", "message", "retryable", "correlation_id"} <= set(resp) and (code is None or resp["code"] == code)


def set_req(strategy="buy_and_hold", key="pst-key-0001", **extra):
    return {"action": "set", "strategy_id": strategy, "idempotency_key": key, "confirmed_by_user": True, "synthetic": True, **extra}


def clear_req(key="pst-key-0002", **extra):
    return {"action": "clear", "idempotency_key": key, "confirmed_by_user": True, "synthetic": True, **extra}


def model_calls(o):
    return o.jobs.count("get_production_strategy") + o.jobs.count("put_production_strategy")


# ================================================================ 1.1 catalog
def test_catalog_entry_and_pinned_schemas():
    load_all()
    e = CATALOG[TOOL]
    assert e.state_changing and e.role_class == "plan-writer" and e.producers == ("financemodel",)
    assert e.min_producer_contract == "1.1.0" and contract_version() == "1.4.0"
    spec = get_tool(TOOL)
    assert spec is not None and spec.input_schema_id == schema_id("tools/production-strategy-request")
    assert spec.input_schema_id.endswith("/core/v1/tools/production-strategy-request.json")
    assert spec.output_schema_id.endswith("/core/v1/tools/production-strategy-response.json")


def test_describe_capabilities_lists_the_tool(offline, invoke):
    tools = {t["name"]: t for t in invoke(offline, "describe_capabilities", {})["tools"]}
    assert tools[TOOL]["available"] is True and tools[TOOL]["state_changing"] is True


# ================================================================ PST-01 get
def test_pst01_get_none(offline, invoke):
    resp = invoke(offline, TOOL, {"action": "get", "synthetic": True})
    assert resp == {"environment": "beta", "action": "get", "strategy": None, "changed": False, "synthetic": True}
    assert offline.jobs.count("get_production_strategy") == 1 and offline.jobs.count("put_production_strategy") == 0


def test_pst01_get_needs_no_idempotency_key_or_confirmation(offline, invoke):
    offline.jobs.production_strategy = json.loads(json.dumps({"strategy_id": "buy_and_hold", "environment": "beta", "selected_at": "2026-01-09T15:00:00Z", "selected_by": "synthetic-operator"}))
    resp = invoke(offline, TOOL, {"action": "get"})
    assert resp["strategy"]["strategy_id"] == "buy_and_hold" and resp["changed"] is False
    assert validate_document(resp, CATALOG[TOOL].output_schema).valid


def test_get_of_another_environment_is_internal(offline, invoke):
    offline.jobs.production_strategy = {"strategy_id": "buy_and_hold", "environment": "gamma", "selected_at": "2026-01-09T15:00:00Z", "selected_by": "synthetic-operator"}
    assert is_error(invoke(offline, TOOL, {"action": "get"}), "INTERNAL")


# ================================================================ PST-02 confirmed user actions
def test_pst02_set_get_clear_round_trip(offline, invoke):
    first = invoke(offline, TOOL, set_req())
    assert not is_error(first), first
    assert first["action"] == "set" and first["changed"] is True and first["strategy"]["strategy_id"] == "buy_and_hold"
    assert validate_document(first, CATALOG[TOOL].output_schema).valid
    put = [c for c in offline.jobs.calls if c.op == "put_production_strategy"][-1]
    assert put.method == "PUT" and put.body["confirmed_by_user"] is True and put.body["action"] == "set"
    assert re.match(DERIVED_KEY_PATTERN, put.body["idempotency_key"]) and put.body["idempotency_key"] != "pst-key-0001"
    assert "contract_version" not in put.body
    assert json.loads(put.headers["X-Finplan-Caller"])["channel"] == "direct_test"
    # a repeat with the same key returns the original result (producer idempotency on the derived key)
    assert invoke(offline, TOOL, set_req()) == first
    assert invoke(offline, TOOL, {"action": "get"})["strategy"]["strategy_id"] == "buy_and_hold"
    cleared = invoke(offline, TOOL, clear_req())
    assert cleared["strategy"] is None and cleared["changed"] is True
    assert invoke(offline, TOOL, {"action": "get"})["strategy"] is None


@pytest.mark.parametrize("request_", [set_req(), clear_req()], ids=["set", "clear"])
@pytest.mark.parametrize("confirmation", [None, False])
def test_pst02_missing_confirmation_makes_no_call(offline, invoke, request_, confirmation):
    req = dict(request_)
    if confirmation is None:
        req.pop("confirmed_by_user")
    else:
        req["confirmed_by_user"] = confirmation
    resp = invoke(offline, TOOL, req)
    assert is_error(resp, "PRECONDITION_FAILED") and resp["details"]["reason"] == "confirmation_required"
    assert model_calls(offline) == 0


def test_pst02_non_boolean_confirmation_is_validation_failed(offline, invoke):
    resp = invoke(offline, TOOL, set_req(confirmed_by_user="yes"))
    assert is_error(resp, "VALIDATION_FAILED") and model_calls(offline) == 0


def test_pst02_write_needs_idempotency_key(offline, invoke):
    req = set_req()
    req.pop("idempotency_key")
    assert is_error(invoke(offline, TOOL, req), "VALIDATION_FAILED") and model_calls(offline) == 0


def test_pst02_researcher_only_caller_is_forbidden():
    load_all()
    spec = get_tool(TOOL)
    researcher = SimpleNamespace(groups=frozenset({"researcher"}))
    from finplan_tools.core.errors import ToolError

    with pytest.raises(ToolError) as exc:
        spec.authorize(researcher, {"action": "set", "strategy_id": "buy_and_hold", "idempotency_key": "k-00000001"}, {"confirmed_by_user": True})
    assert exc.value.code == "FORBIDDEN"
    spec.authorize(researcher, {"action": "get"}, {})  # get: any authenticated caller


def test_pst02_gateway_caller_without_verified_groups_is_forbidden(offline):
    """Gateway callers carry no verified groups until FA-OQ-2 / LT-OQ-1 close: set fails closed."""

    class Ctx:
        class client_context:
            custom = {"bedrockAgentCoreToolName": "tools___production_strategy"}

    load_all()
    resp = execute(get_tool(TOOL), set_req(), Ctx(), offline.runtime)
    assert is_error(resp, "FORBIDDEN") and resp["details"]["reason"] == "group_required"
    assert model_calls(offline) == 0
    got = execute(get_tool(TOOL), {"action": "get"}, Ctx(), offline.runtime)
    assert got["strategy"] is None


def test_prod_direct_writes_refused():
    o = offline_runtime("prod")
    from finplan_tools.handler import invoke as _invoke

    resp = _invoke(TOOL, o.event(set_req()), None, o.runtime)
    assert is_error(resp, "FORBIDDEN") and model_calls(o) == 0


# ================================================================ PST-03 FinanceModel decides validity
def test_pst03_no_evaluation_evidence_passes_through(offline, invoke):
    resp = invoke(offline, TOOL, set_req("momentum_12_1"))
    assert is_error(resp, "VALIDATION_FAILED") and resp["details"]["reason"] == "no_evaluation_evidence" and resp["details"]["pointer"] == "/strategy_id"
    assert offline.jobs.count("put_production_strategy") == 1 and offline.jobs.production_strategy is None


@pytest.mark.parametrize("code", ["BUDGET_EXCEEDED", "CONFLICT", "IDEMPOTENCY_KEY_REUSED", "RATE_LIMITED"])
def test_pst03_producer_errors_pass_through(offline, invoke, code):
    offline.jobs.fail_next("put_production_strategy", code, details={"reason": "injected"})
    resp = invoke(offline, TOOL, set_req())
    assert is_error(resp, code) and resp["details"]["reason"] == "injected"


def test_dependency_unavailable_before_financemodel_1_1(offline, invoke):
    """FinanceModel's release in the environment predates the selection operations."""
    doc = release_manifest("financemodel", "beta")
    doc["contract_version"] = "1.0.0"
    offline.set_param("financemodel", "release", "manifest", doc)
    for req in ({"action": "get"}, set_req()):
        resp = invoke(offline, TOOL, req)
        assert is_error(resp, "DEPENDENCY_UNAVAILABLE") and resp["details"]["reason"] == "operation_not_released" and resp["retryable"] is False
    tools = {t["name"]: t for t in invoke(offline, "describe_capabilities", {})["tools"]}
    assert tools[TOOL]["available"] is False and tools[TOOL]["unavailable_reason"] == "DEPENDENCY_UNAVAILABLE"
    assert tools["get_job_status"]["available"] is True
    assert model_calls(offline) == 0


def test_dependency_unavailable_without_financemodel(offline, invoke):
    offline.remove_model()
    assert is_error(invoke(offline, TOOL, {"action": "get"}), "DEPENDENCY_UNAVAILABLE") and model_calls(offline) == 0


def test_tool_never_submits_jobs_or_touches_the_platform(offline, invoke):
    invoke(offline, TOOL, set_req())
    invoke(offline, TOOL, clear_req())
    assert offline.jobs.count("submit_job") == 0 and offline.platform.count() == 0


# ================================================================ PST-04 no side doors (policy simulation)
def _allowed(env, action, resource):
    apis = ProducerApis("planapi001", "live", "planapi001", "live", "jobsapi001", "api")
    policy = role_class_policy(env, "plan-writer", apis=apis, **KW)
    boundary = contract_boundaries.env_permission_boundary(env, **KW)
    return evaluate(Request(action, resource, {}), {"identity": policy}, boundary).allowed


@pytest.mark.parametrize("env", ["beta", "gamma", "prod"])
def test_pst04_plan_writer_cannot_write_financemodel_config(env):
    key = f"arn:aws:ssm:{REGION}:{ACCOUNT}:parameter/finplan/{env}/financemodel/config/production-strategy"
    for action in ("ssm:PutParameter", "ssm:DeleteParameter", "ssm:LabelParameterVersion"):
        assert not _allowed(env, action, key), action
    policy = role_class_policy(env, "plan-writer", **KW)
    deny = next(s for s in policy["Statement"] if s["Sid"] == "DenyFinanceModelConfigWrites")
    assert deny["Effect"] == "Deny" and "ssm:PutParameter" in deny["Action"]


def test_pst04_plan_writer_reaches_only_the_strategy_operations():
    job = lambda m, p: f"arn:aws:execute-api:{REGION}:{ACCOUNT}:jobsapi001/api/{m}/{p}"
    assert _allowed("beta", "execute-api:Invoke", job("GET", "v1/production-strategy"))
    assert _allowed("beta", "execute-api:Invoke", job("PUT", "v1/production-strategy"))
    assert not _allowed("beta", "execute-api:Invoke", job("POST", "v1/jobs"))
    assert not _allowed("beta", "execute-api:Invoke", job("DELETE", "v1/production-strategy"))
    assert not _allowed("beta", "execute-api:Invoke", job("POST", "v1/jobs/run_1/approve"))
    for role in ("reader", "submitter"):
        policy = role_class_policy("beta", role, apis=ProducerApis("planapi001", "live", "planapi001", "live", "jobsapi001", "api"), **KW)
        boundary = contract_boundaries.env_permission_boundary("beta", **KW)
        assert not evaluate(Request("execute-api:Invoke", job("PUT", "v1/production-strategy"), {}), {"identity": policy}, boundary).allowed


# ================================================================ 1.4 documented example
def test_documented_examples_validate():
    text = (ROOT / "docs" / "tools.md").read_text(encoding="utf-8")
    blocks = re.findall(r"```json\n(.*?)```", text, flags=re.S)
    requests = [json.loads(b) for b in blocks if '"action"' in b and '"environment"' not in b]
    responses = [json.loads(b) for b in blocks if '"environment"' in b]
    assert requests and responses
    for req in requests:
        body = {k: v for k, v in req.items() if k != "confirmed_by_user"}
        assert validate_document(body, CATALOG[TOOL].input_schema).valid, req
    for resp in responses:
        assert validate_document(resp, CATALOG[TOOL].output_schema).valid, resp
