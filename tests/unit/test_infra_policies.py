"""IAM policy simulation of the role classes and pipeline roles (task 8.4; ENVW-02, ENV-03, ENV-05,
EXP-07, PLN-08). Requests are evaluated offline with the contract evaluator, together with the
contract permission boundary each role carries; the account is a placeholder."""

from __future__ import annotations

import pytest
from finplan_contracts import boundaries as contract_boundaries
from finplan_contracts.iam import Request, evaluate

from finplan_tools.core.registry import CATALOG
from infra.stacks import naming as n
from infra.stacks.policies import ProducerApis, deploy_execution_statements, role_class_policy, stage_role_statements, tool_reference_names
from tests.unit.infra_support import ACCOUNT, PARTITION, REGION, STORE_ARN

KW = {"partition": PARTITION, "region": REGION, "account": ACCOUNT}
#: Distinct API IDs per environment (one account holds all three).
APIS = {
    "beta": ProducerApis("betaplan01", "live", "betaplan01", "live", "betajobs01", "api"),
    "gamma": ProducerApis("gammaplan1", "live", "gammaplan1", "live", "gammajobs1", "api"),
    "prod": ProducerApis("prodplan01", "live", "prodplan01", "live", "prodjobs01", "api"),
}


def api(env: str, method: str, path: str, *, job: bool = False) -> str:
    a = APIS[env]
    api_id, stage = (a.job_id, a.job_stage) if job else (a.plan_id, a.plan_stage)
    return f"arn:aws:execute-api:{REGION}:{ACCOUNT}:{api_id}/{stage}/{method}/{path}"


def allowed(env: str, cls: str, action: str, resource: str, **ctx) -> bool:
    policy = role_class_policy(env, cls, apis=APIS[env], **KW)
    boundary = contract_boundaries.env_permission_boundary(env, **KW)
    return evaluate(Request(action, resource, ctx), {"identity": policy}, boundary).allowed


@pytest.mark.parametrize("cls", ["reader", "submitter", "plan-writer"])
def test_gamma_role_reaches_gamma_and_never_prod(cls):
    """ENVW-02: a gamma tool role -> prod plan API is denied in simulation (single account)."""
    assert allowed("gamma", cls, "execute-api:Invoke", api("gamma", "GET", "v1/plan-versions/pv_1"))
    assert not allowed("gamma", cls, "execute-api:Invoke", api("prod", "GET", "v1/plan-versions/pv_1"))
    assert not allowed("gamma", cls, "execute-api:Invoke", api("beta", "GET", "v1/plans/pl_1"))
    assert not allowed("gamma", cls, "execute-api:Invoke", api("prod", "GET", "v1/jobs/run_1", job=True))
    # prod-tagged and prod-named resources are denied by the boundary and the explicit deny
    assert not allowed("gamma", cls, "ssm:GetParameter", f"arn:aws:ssm:{REGION}:{ACCOUNT}:parameter/finplan/prod/financialplanning/api/plan-endpoint")
    assert not allowed("gamma", cls, "ssm:GetParameter", f"arn:aws:ssm:{REGION}:{ACCOUNT}:parameter/finplan/gamma/x", **{"aws:ResourceTag/environment": "prod"})
    assert not allowed("gamma", cls, "lambda:InvokeFunction", f"arn:aws:lambda:{REGION}:{ACCOUNT}:function:finplan-prod-financelambdastool-get-plan")


@pytest.mark.parametrize("env", ["beta", "gamma", "prod"])
def test_reads_only_own_references(env):
    for path in tool_reference_names(env):
        assert path.startswith(f"/finplan/{env}/")
        assert allowed(env, "reader", "ssm:GetParameter", f"arn:aws:ssm:{REGION}:{ACCOUNT}:parameter{path}")
    assert not allowed(env, "reader", "ssm:GetParameter", f"arn:aws:ssm:{REGION}:{ACCOUNT}:parameter/finplan/{env}/financialplanning/config/anything-else")
    assert not allowed(env, "submitter", "ssm:PutParameter", f"arn:aws:ssm:{REGION}:{ACCOUNT}:parameter/finplan/{env}/financelambdastool/config/tool-limits")


def test_reader_is_get_only():
    assert allowed("beta", "reader", "execute-api:Invoke", api("beta", "GET", "v1/snapshots/snap_1/observations"))
    assert allowed("beta", "reader", "execute-api:Invoke", api("beta", "GET", "v1/jobs/run_1/result", job=True))
    for method, path in (("POST", "v1/plans/pl_1/versions"), ("POST", "v1/ingestions"), ("POST", "v1/plans/pl_1/publications")):
        assert not allowed("beta", "reader", "execute-api:Invoke", api("beta", method, path))
    assert not allowed("beta", "reader", "execute-api:Invoke", api("beta", "POST", "v1/jobs", job=True))


def test_submitter_and_plan_writer_routes():
    assert allowed("beta", "submitter", "execute-api:Invoke", api("beta", "POST", "v1/ingestions"))
    assert allowed("beta", "submitter", "execute-api:Invoke", api("beta", "POST", "v1/jobs", job=True))
    assert not allowed("beta", "submitter", "execute-api:Invoke", api("beta", "POST", "v1/plans/pl_1/versions"))
    for path in ("v1/plans/pl_1/versions", "v1/plan-versions/pv_1/validate", "v1/plans/pl_1/publications"):
        assert allowed("beta", "plan-writer", "execute-api:Invoke", api("beta", "POST", path))
    assert not allowed("beta", "plan-writer", "execute-api:Invoke", api("beta", "POST", "v1/ingestions"))
    assert not allowed("beta", "plan-writer", "execute-api:Invoke", api("beta", "POST", "v1/jobs", job=True))


@pytest.mark.parametrize("cls", ["reader", "submitter", "plan-writer"])
def test_explicit_denies(cls):
    """Execution routes (PLN-08), approve/cancel (EXP-07), SageMaker, storage, Lambda invocation."""
    assert not allowed("beta", cls, "execute-api:Invoke", api("beta", "POST", "v1/publications/pub_1/executions"))
    assert not allowed("beta", cls, "execute-api:Invoke", api("beta", "GET", "v1/executions/ex_1"))
    assert not allowed("beta", cls, "execute-api:Invoke", api("beta", "POST", "v1/jobs/run_1/approve", job=True))
    assert not allowed("beta", cls, "execute-api:Invoke", api("beta", "POST", "v1/jobs/run_1/cancel", job=True))
    for action in ("sagemaker:CreateProcessingJob", "s3:GetObject", "dynamodb:PutItem", "lambda:InvokeFunction", "iam:PassRole"):
        assert not allowed("beta", cls, action, "*"), action


def test_absent_job_api_matches_nothing():
    apis = ProducerApis("betaplan01", "live", "betaplan01", "live")  # FinanceModel not released: job_id none
    policy = role_class_policy("beta", "submitter", apis=apis, **KW)
    boundary = contract_boundaries.env_permission_boundary("beta", **KW)
    assert not evaluate(Request("execute-api:Invoke", api("beta", "POST", "v1/jobs", job=True)), {"p": policy}, boundary).allowed


def test_every_tool_role_class_has_its_routes():
    """Each catalog tool's role class can reach what its producer calls need (D1 table)."""
    needs = {
        "query_market_data": [("GET", "v1/snapshots/s/observations", False)],
        "get_plan": [("GET", "v1/plans/p", False)],
        "list_plan_versions": [("GET", "v1/plans/p/versions", False)],
        "get_plan_version": [("GET", "v1/plan-versions/v", False)],
        "get_job_status": [("GET", "v1/jobs/r", True)],
        "get_experiment_result": [("GET", "v1/jobs/r/result", True)],
        "refresh_market_data": [("POST", "v1/ingestions", False)],
        "submit_experiment": [("GET", "v1/snapshots/s", False), ("POST", "v1/jobs", True)],
        "create_override_version": [("GET", "v1/portfolios/x", False), ("POST", "v1/plans/p/versions", False)],
        "validate_plan_version": [("POST", "v1/plan-versions/v/validate", False)],
        "publish_plan_version": [("POST", "v1/plans/p/publications", False)],
    }
    for tool, calls in needs.items():
        for method, path, job in calls:
            assert allowed("gamma", CATALOG[tool].role_class, "execute-api:Invoke", api("gamma", method, path, job=job)), (tool, path)


def test_stage_role_invokes_own_tools_only():
    st = {"Version": "2012-10-17", "Statement": stage_role_statements("gamma", STORE_ARN, **KW)}
    boundary = contract_boundaries.env_permission_boundary("gamma", **KW)
    own = f"arn:aws:lambda:{REGION}:{ACCOUNT}:function:{n.function_name('gamma', 'get_plan')}:current"
    prod = f"arn:aws:lambda:{REGION}:{ACCOUNT}:function:{n.function_name('prod', 'get_plan')}:current"
    assert evaluate(Request("lambda:InvokeFunction", own), {"s": st}, boundary).allowed
    assert not evaluate(Request("lambda:InvokeFunction", prod), {"s": st}, boundary).allowed
    assert not evaluate(Request("ssm:GetParameter", f"arn:aws:ssm:{REGION}:{ACCOUNT}:parameter/finplan/prod/financialplanning/release/manifest"), {"s": st}, boundary).allowed
    assert evaluate(Request("ssm:PutParameter", f"arn:aws:ssm:{REGION}:{ACCOUNT}:parameter/finplan/gamma/financelambdastool/contract/tool-catalog"), {"s": st}, boundary).allowed
    assert not evaluate(Request("ssm:PutParameter", f"arn:aws:ssm:{REGION}:{ACCOUNT}:parameter/finplan/gamma/financialplanning/api/plan-endpoint"), {"s": st}, boundary).allowed


def test_exec_role_creates_roles_only_with_the_boundary():
    st = {"Version": "2012-10-17", "Statement": deploy_execution_statements("beta", STORE_ARN, **KW)}
    role = f"arn:aws:iam::{ACCOUNT}:role/{n.role_class_role_name('beta', 'reader')}"
    good = {"iam:PermissionsBoundary": f"arn:aws:iam::{ACCOUNT}:policy/finplan-beta-permission-boundary"}
    assert evaluate(Request("iam:CreateRole", role, good), {"s": st}).allowed
    assert not evaluate(Request("iam:CreateRole", role, {}), {"s": st}).allowed
    assert not evaluate(Request("iam:CreateRole", f"arn:aws:iam::{ACCOUNT}:role/finplan-gamma-financelambdastool-tool-role-reader", good), {"s": st}).allowed
    assert not evaluate(Request("lambda:CreateFunction", f"arn:aws:lambda:{REGION}:{ACCOUNT}:function:finplan-beta-financialplanning-x"), {"s": st}).allowed


def test_policies_fit_the_inline_size_limit():
    import json
    import re

    for env in ("beta", "gamma", "prod"):
        for cls in ("reader", "submitter", "plan-writer"):
            doc = role_class_policy(env, cls, apis=APIS[env], **KW)
            assert len(re.sub(r"\s", "", json.dumps(doc, separators=(",", ":")))) < 10240
