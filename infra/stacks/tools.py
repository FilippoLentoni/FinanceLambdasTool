"""Per-environment tool stack ``finplan-<env>-financelambdastool-tools`` (tasks 8.1-8.3; spec
tool-environment-wiring; matrix row ``mcp-adapter-lambdas``).

* **Three role classes** (D1): ``finplan-<env>-financelambdastool-tool-role-{reader,submitter,plan-writer}``,
  trusted by Lambda only, carrying the environment permission boundary (:class:`infra.stacks.common.EnvStack`)
  and the identity policies of :func:`infra.stacks.policies.role_class_policy`.
* **One function per tool** from ONE code asset (:mod:`infra.stacks.lambda_code`): Python 3.12,
  ``arm64``, 256 MB, no VPC, no provisioned or reserved concurrency, per-tool timeout from
  ``tool-limits`` defaults, handler ``finplan_tools.handler.handler`` with ``FINPLAN_ENV``,
  ``FINPLAN_TOOL_NAME`` and ``FINPLAN_RELEASE_ID`` only (no endpoint, ARN or account: producers are
  resolved at run time from same-environment SSM). Each function has the alias ``current`` on the
  version of this release and an explicit 30-day log group ``/aws/lambda/<function>`` (lesson L6;
  it references its function, so the ownership check attributes it to the function's row).
* **Invoke grants on the alias only** (ENVW-03, ENVW-04, ENVW-08), never a wildcard:

  - this repository's pipeline stage role (``finplan-<env>-financelambdastool-pipeline-stage-role``,
    created by the bootstrap): every tool in beta and gamma, read-only tools in prod (smoke);
  - the single direct-test principal (stack parameter ``DirectTestPrincipal``, resolved by the
    pre-deploy step from ``/finplan/<env>/financelambdastool/config/direct-test-principal-name``
    and validated there; ``none`` when the parameter is absent -> no grant): every tool in beta and
    gamma, read-only tools in prod;
  - the FinanceAgent Gateway service role (``GatewayPrincipalRoleName``, from
    ``/finplan/<env>/financeagent/agent/gateway-principal-ref``; ``none`` -> no grant).

* **Producer API identifiers** (``PlanApiId``/``PlanApiStage``, ``IngestionApiId``/``IngestionApiStage``,
  ``JobApiId``/``JobApiStage``) are stack parameters the pre-deploy step resolves from this
  environment's SSM endpoints (8.2), so ``execute-api:Invoke`` names only this environment's APIs
  (ENVW-02). ``JobApiId=none`` (FinanceModel not released) matches no API.
* **Outputs** (published to SSM by the release step, never by this template): the alias-qualified
  ARN of every tool (``<Tool>Ref``) and every role class (``Role<Class>Ref``).

All parameter defaults and allowed patterns are environment-specific, so a gamma stack cannot be
given a beta or prod Gateway role.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

import aws_cdk as cdk
from aws_cdk import Aws, CfnCondition, CfnParameter, Duration, Fn
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_logs as logs

from finplan_tools.core.config import DEFAULT_TOOL_LIMITS
from finplan_tools.core.registry import CATALOG, ROLE_CLASSES, CatalogEntry

from . import naming as n
from .common import EnvStack, metadata_role, tag_role
from .lambda_code import function_code
from .policies import ProducerApis, role_class_policy

__all__ = [
    "HANDLER",
    "LAMBDA_ARCHITECTURE",
    "MEMORY_MB",
    "PARAMETERS",
    "ToolsStack",
    "add_to_app",
    "output_key",
    "role_output_key",
]

HANDLER = "finplan_tools.handler.handler"
LAMBDA_ARCHITECTURE = lambda_.Architecture.ARM_64
MEMORY_MB = 256
NONE = "none"
API_ID_PATTERN = "^[a-z0-9]{10}$"
API_STAGE_PATTERN = "^[A-Za-z0-9_-]{1,128}$"
#: Stack parameter -> pre-deploy variable (``scripts/predeploy.py`` exports these names).
PARAMETERS: dict[str, str] = {
    "PlanApiId": "PLAN_API_ID",
    "PlanApiStage": "PLAN_API_STAGE",
    "IngestionApiId": "INGESTION_API_ID",
    "IngestionApiStage": "INGESTION_API_STAGE",
    "JobApiId": "JOB_API_ID",
    "JobApiStage": "JOB_API_STAGE",
    "DirectTestPrincipal": "DIRECT_TEST_PRINCIPAL",
    "GatewayPrincipalRoleName": "GATEWAY_PRINCIPAL_ROLE_NAME",
}


def _camel(text: str) -> str:
    return "".join(p.capitalize() for p in text.replace("_", "-").split("-"))


def output_key(tool: str) -> str:
    """Stack output holding the alias-qualified ARN of ``tool``."""
    return f"{_camel(tool)}Ref"


def role_output_key(role_class: str) -> str:
    return f"Role{_camel(role_class)}Ref"


def gateway_role_pattern(env: str) -> str:
    return f"^({NONE}|finplan-{env}-financeagent-[A-Za-z0-9+=,.@_-]{{1,40}})$"


#: ``role/<name>`` or ``user/<name>`` (the pre-deploy step normalises the SSM name to this form).
DIRECT_TEST_PATTERN = f"^({NONE}|(role|user)/[A-Za-z0-9+=,.@_-]{{1,64}})$"


class ToolsStack(EnvStack):
    def __init__(self, scope: Any, construct_id: str, *, env_name: str, release_id: str | None = None, tools: Iterable[str] | None = None, **kwargs: Any) -> None:
        super().__init__(scope, construct_id, env_name=env_name, description=f"FinanceLambdasTool {env_name}: MCP adapter Lambdas (one per tool), role classes and explicit invoke grants", stack_name=n.stack_name(env_name), **kwargs)
        env = env_name
        self.release_id = release_id
        self.roles: dict[str, iam.Role] = {}
        self.functions: dict[str, lambda_.Function] = {}
        self.aliases: dict[str, lambda_.Alias] = {}
        p, a = Aws.PARTITION, Aws.ACCOUNT_ID

        params = self._parameters(env)
        apis = ProducerApis(
            plan_id=params["PlanApiId"].value_as_string,
            plan_stage=params["PlanApiStage"].value_as_string,
            ingestion_id=params["IngestionApiId"].value_as_string,
            ingestion_stage=params["IngestionApiStage"].value_as_string,
            job_id=params["JobApiId"].value_as_string,
            job_stage=params["JobApiStage"].value_as_string,
        )
        has_direct = CfnCondition(self, "HasDirectTestPrincipal", expression=Fn.condition_not(Fn.condition_equals(params["DirectTestPrincipal"].value_as_string, NONE)))
        has_gateway = CfnCondition(self, "HasGatewayPrincipal", expression=Fn.condition_not(Fn.condition_equals(params["GatewayPrincipalRoleName"].value_as_string, NONE)))

        # ---------------------------------------------------------------- role classes
        for cls in ROLE_CLASSES:
            role = iam.Role(
                self,
                f"Role{_camel(cls)}",
                role_name=n.role_class_role_name(env, cls),
                assumed_by=iam.ServicePrincipal("lambda.amazonaws.com", conditions={"StringEquals": {"aws:SourceAccount": a}}),
                description=f"FinanceLambdasTool {env} tool role class {cls} (D1)",
                inline_policies={f"tool-{cls}": iam.PolicyDocument.from_json(role_class_policy(env, cls, apis=apis, partition=p, region=Aws.REGION, account=a))},
                max_session_duration=Duration.hours(1),
            )
            tag_role(role, n.ROLE_CLASS_LOGICAL_ROLES[cls])
            self.roles[cls] = role
            cdk.CfnOutput(self, role_output_key(cls), value=role.role_arn, description=f"Published at /finplan/<env>/financelambdastool/lambda/role-{cls}-arn")

        # ---------------------------------------------------------------- functions
        code = function_code()
        stage_principal = f"arn:{p}:iam::{a}:role/{n.stage_role_name(env)}"
        direct_arn = Fn.sub("arn:${AWS::Partition}:iam::${AWS::AccountId}:${Principal}", {"Principal": params["DirectTestPrincipal"].value_as_string})
        gateway_arn = Fn.sub("arn:${AWS::Partition}:iam::${AWS::AccountId}:role/${Name}", {"Name": params["GatewayPrincipalRoleName"].value_as_string})
        selected = sorted(tools) if tools is not None else sorted(CATALOG)
        for tool in selected:
            entry = CATALOG[tool]
            _fn, alias = self._function(env, entry, code)
            cid = _camel(tool)
            granted_to_owner_and_tests = env != "prod" or entry.prod_direct_test
            if granted_to_owner_and_tests:
                lambda_.CfnPermission(self, f"{cid}PipelineTestInvoke", action="lambda:InvokeFunction", function_name=alias.function_arn, principal=stage_principal)
                perm = lambda_.CfnPermission(self, f"{cid}DirectTestInvoke", action="lambda:InvokeFunction", function_name=alias.function_arn, principal=direct_arn)
                perm.cfn_options.condition = has_direct
            gw = lambda_.CfnPermission(self, f"{cid}GatewayInvoke", action="lambda:InvokeFunction", function_name=alias.function_arn, principal=gateway_arn)
            gw.cfn_options.condition = has_gateway
            cdk.CfnOutput(self, output_key(tool), value=alias.function_arn, description=f"Published at /finplan/<env>/financelambdastool/lambda/{entry.lambda_ref_name}")

    # ------------------------------------------------------------------ helpers
    def _parameters(self, env: str) -> dict[str, CfnParameter]:
        out: dict[str, CfnParameter] = {}
        for prefix, label in (("Plan", "plan API (/finplan/<env>/financialplanning/api/plan-endpoint)"), ("Ingestion", "ingestion route (/finplan/<env>/financialplanning/api/ingestion-endpoint)")):
            out[f"{prefix}ApiId"] = CfnParameter(self, f"{prefix}ApiId", type="String", allowed_pattern=API_ID_PATTERN, description=f"REST API ID of this environment's {label}; resolved by the pre-deploy step")
            out[f"{prefix}ApiStage"] = CfnParameter(self, f"{prefix}ApiStage", type="String", allowed_pattern=API_STAGE_PATTERN, description=f"Stage of this environment's {label}")
        out["JobApiId"] = CfnParameter(self, "JobApiId", type="String", default=NONE, allowed_pattern=f"^({NONE}|[a-z0-9]{{10}})$", description="REST API ID of this environment's FinanceModel job API (/finplan/<env>/financemodel/api/job-endpoint); none while FinanceModel is not released")
        out["JobApiStage"] = CfnParameter(self, "JobApiStage", type="String", default=NONE, allowed_pattern=API_STAGE_PATTERN, description="Stage of the FinanceModel job API (none while absent)")
        out["DirectTestPrincipal"] = CfnParameter(self, "DirectTestPrincipal", type="String", default=NONE, allowed_pattern=DIRECT_TEST_PATTERN, description="role/<name> or user/<name> of the single direct-test principal (validated name from /finplan/<env>/financelambdastool/config/direct-test-principal-name); none -> no direct-test grant")
        out["GatewayPrincipalRoleName"] = CfnParameter(self, "GatewayPrincipalRoleName", type="String", default=NONE, allowed_pattern=gateway_role_pattern(env), description="This environment's FinanceAgent Gateway service role name (/finplan/<env>/financeagent/agent/gateway-principal-ref); none -> no Gateway grant")
        return out

    def _function(self, env: str, entry: CatalogEntry, code: lambda_.Code) -> tuple[lambda_.Function, lambda_.Alias]:
        cid = _camel(entry.name)
        env_vars = {"FINPLAN_ENV": env, "FINPLAN_TOOL_NAME": entry.name, "FINPLAN_ACCOUNT_ID": Aws.ACCOUNT_ID}
        if self.release_id:
            env_vars["FINPLAN_RELEASE_ID"] = self.release_id
        fn = lambda_.Function(
            self,
            cid,
            function_name=n.function_name(env, entry.name),
            runtime=lambda_.Runtime.PYTHON_3_12,
            architecture=LAMBDA_ARCHITECTURE,
            handler=HANDLER,
            code=code,
            role=self.roles[entry.role_class],
            memory_size=MEMORY_MB,
            timeout=Duration.seconds(int(DEFAULT_TOOL_LIMITS["timeouts_seconds"][entry.timeout_key])),
            environment=env_vars,
            description=f"FinanceLambdasTool {env} tool {entry.name} ({'state-changing' if entry.state_changing else 'read-only'}; role class {entry.role_class})",
        )
        tag_role(fn, "tool-lambda")
        version = fn.current_version
        metadata_role(version.node.default_child, "tool-lambda")  # type: ignore[arg-type]
        alias = lambda_.Alias(self, f"{cid}Alias", alias_name=n.TOOL_ALIAS, version=version, description=f"{entry.name}: the version of the deployed release")
        metadata_role(alias.node.default_child, "tool-lambda-alias")  # type: ignore[arg-type]
        group = logs.CfnLogGroup(self, f"{cid}LogGroup", log_group_name=f"/aws/lambda/{fn.function_name}", retention_in_days=n.LOG_RETENTION_DAYS)
        group.apply_removal_policy(cdk.RemovalPolicy.DESTROY)
        self.functions[entry.name] = fn
        self.aliases[entry.name] = alias
        return fn, alias


def add_to_app(app: cdk.App, envs: list[str], *, synthesizer_factory: Any = None) -> list[cdk.Stack]:
    """``infra/app.py`` hook: one tool stack per selected environment, then the account-level
    store, tooling and pipeline stacks (when all three environments are selected)."""
    from .tooling import deployment_synthesizer, get_tooling_stack

    release_id = app.node.try_get_context("release_id")
    make = synthesizer_factory or deployment_synthesizer
    stacks: list[cdk.Stack] = []
    tool_stacks: dict[str, ToolsStack] = {}
    for env in envs:
        stage = cdk.Stage(app, env.capitalize())
        st = ToolsStack(stage, "Tools", env_name=env, release_id=release_id, synthesizer=make())
        tool_stacks[env] = st
        stacks.append(st)
    tooling = get_tooling_stack(app)
    stacks += [s for s in (app.node.try_find_child("PipelineStore"), tooling) if isinstance(s, cdk.Stack)]
    from .pipeline import add_pipeline_if_complete

    add_pipeline_if_complete(tooling, tool_stacks)
    return stacks
