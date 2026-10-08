"""IAM documents of every FinanceLambdasTool role (tasks 8.1, 8.4, 9.3; ENVW-02, ENV-03, ENV-05; D1).

Every document is a plain dict built from ``partition``, ``region`` and ``account`` plus the
producer API identifiers:

* the CDK stacks pass CloudFormation tokens (``Aws.PARTITION`` ..., and the ``PlanApiId`` /
  ``JobApiId`` stack parameters the pre-deploy step resolves from same-environment SSM), so no
  account, ARN or endpoint literal is ever written to a file;
* the unit tests pass concrete placeholders and evaluate requests offline with
  :func:`finplan_contracts.iam.evaluate`, together with the contract permission boundary the role
  carries (policy simulation, task 8.4).

Role classes (D1; names in :mod:`infra.stacks.naming`):

============  ==========================================================================
class         may (``execute-api:Invoke`` only on THIS environment's producer API IDs)
============  ==========================================================================
reader        platform ``GET`` plans, plan versions, portfolios, snapshots; job ``GET``
submitter     reader + ``POST /v1/ingestions`` + ``POST /v1/jobs`` (dry run and submit)
plan-writer   reader + version create, validate and publish
============  ==========================================================================

Every class also reads only its own environment's producer references in SSM and writes only its
own log streams (lesson L3), and carries explicit denies: platform execution routes, job
``approve``/``cancel``, any non-GET call for ``reader``, SageMaker, storage and SSM writes, Lambda
invocation, every other environment's named resources (single account) and the live-financial
deny list (ENV-05). The environment permission boundary (``finplan-<env>-permission-boundary``)
adds the tag-based isolation.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from finplan_contracts import boundaries as contract_boundaries
from finplan_contracts import iam as contract_iam

from finplan_tools.core.registry import CATALOG

from . import naming as n

__all__ = [
    "APPROVE_CANCEL_PATHS",
    "EXECUTION_PATHS",
    "JOB_READ_ROUTES",
    "PLAN_WRITER_ROUTES",
    "PLATFORM_READ_ROUTES",
    "ProducerApis",
    "build_role_statements",
    "deploy_execution_statements",
    "role_class_policy",
    "stage_role_statements",
    "tool_reference_names",
]

PARTITION = contract_iam.PARTITION
REGION = contract_iam.REGION
ACCOUNT = contract_iam.ACCOUNT

#: Platform read routes (method GET) the tools use (backends/platform.py).
PLATFORM_READ_ROUTES = ("v1/plans/*", "v1/plan-versions/*", "v1/portfolios/*", "v1/snapshots/*")
#: FinanceModel job reads (``get_job_status``, ``get_job_result``).
JOB_READ_ROUTES = ("v1/jobs/*",)
#: plan-writer routes (D1).
PLAN_WRITER_ROUTES = (("POST", "v1/plans/*/versions"), ("POST", "v1/plan-versions/*/validate"), ("POST", "v1/plans/*/publications"))
#: Platform execution routes, never callable by a tool (D1, PLN-08).
EXECUTION_PATHS = ("*/*/*/v1/publications/*/executions", "*/*/*/v1/executions*")
#: Job approval and cancellation, never callable by a tool (EXP-07, D1).
APPROVE_CANCEL_PATHS = ("*/*/*/v1/jobs/*/approve", "*/*/*/v1/jobs/*/cancel")
_WRITE_METHODS = ("POST", "PUT", "PATCH", "DELETE")


@dataclass(frozen=True)
class ProducerApis:
    """Producer REST API identifiers and stages of ONE environment (resolved from its SSM by the
    pre-deploy step; ``none`` when the producer is absent, which matches no API)."""

    plan_id: str = "planapi000"
    plan_stage: str = "live"
    ingestion_id: str = "planapi000"
    ingestion_stage: str = "live"
    job_id: str = "none"
    job_stage: str = "none"


def _arn(service: str, resource: str, *, partition: str, region: str = "", account: str = "") -> str:
    return f"arn:{partition}:{service}:{region}:{account}:{resource}"


def _api(api_id: str, stage: str, method: str, path: str, *, partition: str, region: str, account: str) -> str:
    return _arn("execute-api", f"{api_id}/{stage}/{method}/{path}", partition=partition, region=region, account=account)


def _param(path: str, *, partition: str, region: str, account: str) -> str:
    return contract_iam.ssm_parameter_arn(path, partition=partition, region=region, account=account)


def tool_reference_names(env: str) -> list[str]:
    """SSM parameters a tool Lambda reads at run time (same environment only; D9)."""
    return [
        f"/finplan/{env}/financialplanning/api/plan-endpoint",
        f"/finplan/{env}/financialplanning/api/ingestion-endpoint",
        f"/finplan/{env}/financialplanning/release/manifest",
        f"/finplan/{env}/financemodel/api/job-endpoint",
        f"/finplan/{env}/financemodel/release/manifest",
        n.own_ssm(env, "config", "tool-limits"),
    ]


def _other_env_named(env: str, *, partition: str, region: str, account: str) -> list[str]:
    out: list[str] = []
    for other in n.ENVIRONMENTS:
        if other != env:
            out += contract_boundaries.named_resource_arns(other, partition=partition, region=region, account=account)
    return out


def role_class_policy(env: str, role_class: str, *, apis: ProducerApis | None = None, partition: str = PARTITION, region: str = REGION, account: str = ACCOUNT) -> dict[str, Any]:
    """Identity policy of ``finplan-<env>-financelambdastool-tool-role-<role_class>``."""
    if env not in n.ENVIRONMENTS:
        raise ValueError(f"unknown environment {env!r}")
    if role_class not in n.ROLE_CLASSES:
        raise ValueError(f"unknown role class {role_class!r}")
    apis = apis or ProducerApis()
    kw = {"partition": partition, "region": region, "account": account}
    groups = [f"/aws/lambda/{n.function_name(env, t)}" for t, e in sorted(CATALOG.items()) if e.role_class == role_class]
    st: list[dict[str, Any]] = [
        {"Sid": "PlatformReads", "Effect": "Allow", "Action": ["execute-api:Invoke"], "Resource": [_api(apis.plan_id, apis.plan_stage, "GET", r, **kw) for r in PLATFORM_READ_ROUTES]},
        {"Sid": "JobReads", "Effect": "Allow", "Action": ["execute-api:Invoke"], "Resource": [_api(apis.job_id, apis.job_stage, "GET", r, **kw) for r in JOB_READ_ROUTES]},
    ]
    if role_class == "submitter":
        st.append({"Sid": "Ingestion", "Effect": "Allow", "Action": ["execute-api:Invoke"], "Resource": [_api(apis.ingestion_id, apis.ingestion_stage, "POST", "v1/ingestions", **kw)]})
        st.append({"Sid": "JobSubmit", "Effect": "Allow", "Action": ["execute-api:Invoke"], "Resource": [_api(apis.job_id, apis.job_stage, "POST", "v1/jobs", **kw)]})
    if role_class == "plan-writer":
        st.append({"Sid": "PlanWrites", "Effect": "Allow", "Action": ["execute-api:Invoke"], "Resource": [_api(apis.plan_id, apis.plan_stage, m, p, **kw) for m, p in PLAN_WRITER_ROUTES]})
    st += [
        {"Sid": "ReadOwnEnvironmentReferences", "Effect": "Allow", "Action": ["ssm:GetParameter"], "Resource": [_param(p, **kw) for p in tool_reference_names(env)]},
        {"Sid": "OwnLogStreams", "Effect": "Allow", "Action": ["logs:CreateLogStream", "logs:PutLogEvents"], "Resource": [_arn("logs", f"log-group:{g}:*", **kw) for g in groups]},
        # ---- explicit denies (they hold even if an allow above were widened)
        {"Sid": "DenyExecutionRoutes", "Effect": "Deny", "Action": ["execute-api:Invoke"], "Resource": [_arn("execute-api", p, **kw) for p in EXECUTION_PATHS]},
        {"Sid": "DenyApproveAndCancel", "Effect": "Deny", "Action": ["execute-api:Invoke"], "Resource": [_arn("execute-api", p, **kw) for p in APPROVE_CANCEL_PATHS]},
    ]
    if role_class == "reader":
        st.append({"Sid": "DenyNonReadCalls", "Effect": "Deny", "Action": ["execute-api:Invoke"], "Resource": [_arn("execute-api", f"*/*/{m}/*", **kw) for m in _WRITE_METHODS]})
    st += [
        {
            "Sid": "DenyComputeStorageAndConfigWrites",
            "Effect": "Deny",
            "Action": ["sagemaker:*", "s3:*", "dynamodb:*", "ssm:PutParameter", "ssm:DeleteParameter", "ssm:DeleteParameters", "ssm:LabelParameterVersion", "lambda:InvokeFunction", "lambda:InvokeAsync", "iam:PassRole"],
            "Resource": "*",
        },
        {"Sid": "DenyOtherEnvironments", "Effect": "Deny", "Action": "*", "Resource": _other_env_named(env, **kw)},
        *contract_boundaries.live_financial_deny_statements(),
    ]
    return {"Version": "2012-10-17", "Statement": st}


# ===================================================================== pipeline roles
def deploy_execution_statements(env: str, store_bucket_arn: str, *, partition: str = PARTITION, region: str = REGION, account: str = ACCOUNT) -> list[dict[str, Any]]:
    """CloudFormation execution role of ``env``: only ``finplan-<env>-financelambdastool-*``
    functions, roles and log groups; roles it creates must carry the environment boundary."""
    prefix = f"finplan-{env}-{n.REPO}-"
    kw = {"partition": partition, "region": region, "account": account}
    role_res = _arn("iam", f"role/{prefix}*", partition=partition, account=account)
    boundary = _arn("iam", f"policy/{contract_boundaries.boundary_name(env)}", partition=partition, account=account)
    return [
        {"Sid": "ReadPublishedAssets", "Effect": "Allow", "Action": ["s3:GetObject", "s3:GetObjectVersion"], "Resource": [f"{store_bucket_arn}/assets/*"]},
        {"Sid": "EnvFunctions", "Effect": "Allow", "Action": ["lambda:*"], "Resource": [_arn("lambda", f"function:{prefix}*", **kw)]},
        {"Sid": "EnvRolesCreateWithBoundary", "Effect": "Allow", "Action": ["iam:CreateRole", "iam:PutRolePermissionsBoundary"], "Resource": [role_res], "Condition": {"StringEquals": {"iam:PermissionsBoundary": boundary}}},
        {
            "Sid": "EnvRolesManage",
            "Effect": "Allow",
            "Action": [
                "iam:GetRole",
                "iam:GetRolePolicy",
                "iam:ListRolePolicies",
                "iam:ListAttachedRolePolicies",
                "iam:ListRoleTags",
                "iam:DeleteRole",
                "iam:PutRolePolicy",
                "iam:DeleteRolePolicy",
                "iam:UpdateRole",
                "iam:UpdateRoleDescription",
                "iam:UpdateAssumeRolePolicy",
                "iam:TagRole",
                "iam:UntagRole",
            ],
            "Resource": [role_res],
        },
        {"Sid": "PassToolRolesToLambda", "Effect": "Allow", "Action": ["iam:PassRole"], "Resource": [role_res], "Condition": {"StringEquals": {"iam:PassedToService": "lambda.amazonaws.com"}}},
        {"Sid": "EnvLogGroups", "Effect": "Allow", "Action": ["logs:*"], "Resource": [_arn("logs", f"log-group:/aws/lambda/{prefix}*", **kw), _arn("logs", f"log-group:/aws/lambda/{prefix}*:*", **kw)]},
        {"Sid": "LogGroupsDescribe", "Effect": "Allow", "Action": ["logs:DescribeLogGroups"], "Resource": ["*"]},
    ]


def stage_role_statements(env: str, store_bucket_arn: str, *, partition: str = PARTITION, region: str = REGION, account: str = ACCOUNT) -> list[dict[str, Any]]:
    """Pre-deploy resolver, release publisher and environment test runner of ``env``."""
    own = f"/finplan/{env}/{n.REPO}"
    kw = {"partition": partition, "region": region, "account": account}
    param = lambda p: _param(p, **kw)
    functions = _arn("lambda", f"function:finplan-{env}-{n.REPO}-*", **kw)
    return [
        {
            "Sid": "PublishOwnReferences",
            "Effect": "Allow",
            "Action": ["ssm:PutParameter", "ssm:AddTagsToResource"],
            "Resource": [param(f"{own}/lambda/*"), param(f"{own}/contract/tool-catalog"), param(f"{own}/release/*"), param(f"{own}/config/budget-enforced-role-names")],
        },
        {"Sid": "ReadEnvAndShared", "Effect": "Allow", "Action": list(contract_iam.SSM_READ_ACTIONS), "Resource": [param(f"/finplan/{env}"), param(f"/finplan/{env}/*"), param("/finplan/shared"), param("/finplan/shared/*")]},
        {"Sid": "ReadStackOutputs", "Effect": "Allow", "Action": ["cloudformation:DescribeStacks"], "Resource": [_arn("cloudformation", f"stack/finplan-{env}-{n.REPO}-*/*", **kw)]},
        {"Sid": "ReleaseLedger", "Effect": "Allow", "Action": ["s3:PutObject", "s3:GetObject"], "Resource": [f"{store_bucket_arn}/releases/*"]},
        {"Sid": "ApprovalRecord", "Effect": "Allow", "Action": ["codepipeline:ListActionExecutions", "codepipeline:GetPipelineExecution"], "Resource": [_arn("codepipeline", n.PIPELINE_NAME, **kw)]},
        # deployed suites: real direct invocation of this environment's tools (lesson L5) and
        # read-only inspection of their configuration and resource policies (ENVW-03)
        {"Sid": "InvokeOwnTools", "Effect": "Allow", "Action": ["lambda:InvokeFunction"], "Resource": [functions]},
        {"Sid": "InspectOwnTools", "Effect": "Allow", "Action": ["lambda:GetFunctionConfiguration", "lambda:GetAlias", "lambda:GetPolicy"], "Resource": [functions]},
        {"Sid": "DenyOtherEnvironments", "Effect": "Deny", "Action": "*", "Resource": _other_env_named(env, **kw)},
    ]


def build_role_statements(store_bucket_arn: str) -> list[dict[str, Any]]:
    """Build stage: publish the content-addressed assets and the release ledger."""
    return [
        {"Sid": "PublishAssetsAndReleases", "Effect": "Allow", "Action": ["s3:PutObject", "s3:GetObject"], "Resource": [f"{store_bucket_arn}/assets/*", f"{store_bucket_arn}/releases/*"]},
        {"Sid": "ListStore", "Effect": "Allow", "Action": ["s3:ListBucket"], "Resource": [store_bucket_arn], "Condition": {"StringLike": {"s3:prefix": ["assets/*", "releases/*"]}}},
    ]
