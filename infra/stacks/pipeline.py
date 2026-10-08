"""The FinanceLambdasTool pipeline (task 9.3; spec tool-release-publication; contracts D6 pipeline standard).

Added to the account-level tooling stack (:mod:`infra.stacks.tooling`), so only the authenticated
bootstrap creates or changes it; the pipeline never updates itself. CodePipeline **V2** with
CodeBuild, stages in the contract order, reusing the deployed FinancialPlanning / FinanceModel pattern:

1. **Source**: CodeConnections source of ``FilippoLentoni/FinanceLambdasTool`` on ``main``. The
   connection is the existing, reused GitHub CodeConnection, referenced through
   ``/finplan/shared/financelambdastool/config/codeconnection-ref`` (written by the bootstrap,
   resolved by CloudFormation; never an ARN in a file). ``#{SourceVariables.CommitId}`` goes to the build.
2. **Build**: ``scripts/build_stage.py``: pre-synth gates (contract pin, configuration, leak scan,
   copied-id, consumer conformance, live-permission scan, tool inventory, S3-client guard, unit and
   contract tests), the arm64 Lambda bundle and its artifact check, ``cdk synth`` **once** (release
   mode: source-only code is refused), post-synth gates (ownership, boundaries, live permissions,
   pipeline structure and scoped deploy roles, cost, bundle architecture, log retention, invoke
   grants), a new ``release_id`` and the artifact digest. Its single output ``BuildOutput`` is the
   only input of every later stage. ``rollback_to_release_id`` re-emits a recorded release instead.
3. **Beta**, 4. **Gamma**, 6. **Prod**, each:

   - ``PreDeploy`` (``scripts/stage_runner.py predeploy``): the contract pin is allowed in the
     environment (0.x is beta-only), the platform release serves the pinned major (FinanceModel
     optional, REL-06), the per-call limits are within ``per_call_max_fraction`` x the shared budget
     allocation (6.2a), and this environment's producer references, Gateway principal and
     direct-test principal name are resolved from **its own** SSM and validated (8.2, 8.3). The
     values leave the action as CodePipeline variables (namespace ``PreDeploy<Env>``), never as an
     artifact, so promotion stays artifact-only;
   - ``DeployTools``: CloudFormation deploy of ``finplan-<env>-financelambdastool-tools`` from
     BuildOutput with those variables as parameter overrides (scoped deploy role, CloudFormation
     execution role);
   - ``PublishRelease`` (Lambda and role references, tool catalog, budget-enforced role names, the
     release manifest and pointer; prod adds the approver and time);
   - the environment suite: ``IntegrationBetaTests`` / ``GammaTests`` (real SigV4 direct
     invocation of the deployed tools as the stage role, lesson L5) / ``SmokeTests`` (read-only).

5. **Approval**: one Manual approval action.

Until the bootstrap's source-stage dry run has passed, the inbound transition into Build is disabled
(``SourceDryRunPassed=false``). Every CodeBuild log group ``/aws/codebuild/<project>`` keeps 30 days
(lesson L6): the pinned ownership matrix row ``pipeline-financelambdastool`` does not list
``AWS::Logs::LogGroup`` yet (CONTRACT GAP ``pipeline-logs``, :mod:`infra.stacks.contract_gaps`), so
until that contract change is pinned the bootstrap creates the groups with the cost tags and the
retention before the first build can run; with the gap switched on they are declared here.

Roles: account-level (``environment=shared``, ``finplan-shared-permission-boundary``)
``pipeline-role`` and ``pipeline-build-project-role``; per environment (``environment=<env>``,
``finplan-<env>-permission-boundary``; matrix row ``pipeline-environment-roles-financelambdastool``)
``deploy-role-<env>``, ``deploy-role-<env>-exec`` and ``finplan-<env>-financelambdastool-pipeline-stage-role``.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import aws_cdk as cdk
from aws_cdk import Annotations, Aws, CfnCondition, CfnParameter, Duration, Fn, Tags
from aws_cdk import aws_codebuild as codebuild
from aws_cdk import aws_codepipeline as codepipeline
from aws_cdk import aws_codepipeline_actions as actions
from aws_cdk import aws_iam as iam
from aws_cdk import aws_logs as logs
from aws_cdk import aws_s3 as s3
from aws_cdk import aws_ssm as ssm
from finplan_contracts import ssm as contract_ssm

from . import naming as n
from .common import tag_role
from .policies import build_role_statements, deploy_execution_statements, stage_role_statements
from .tooling import ToolingStack
from .tools import PARAMETERS

__all__ = [
    "BUILD_STAGE",
    "CONNECTION_PARAMETER",
    "ENV_SUITES",
    "GITHUB_REPOSITORY",
    "NO_ROLLBACK",
    "ROLLBACK_VARIABLE",
    "SOURCE_STAGE",
    "STAGE_NAMES",
    "UV_VERSION",
    "PipelineResources",
    "add_pipeline",
    "add_pipeline_if_complete",
    "build_spec",
    "codebuild_project_names",
    "predeploy_namespace",
    "stage_spec",
    "template_path",
]

ROLLBACK_VARIABLE = "rollback_to_release_id"
NO_ROLLBACK = "none"
SOURCE_STAGE = "Source"
BUILD_STAGE = "Build"
STAGE_NAMES = (SOURCE_STAGE, BUILD_STAGE, "Beta", "Gamma", "Approval", "Prod")
ENV_SUITES = {"beta": "integration-beta", "gamma": "gamma", "prod": "smoke"}
CONNECTION_PARAMETER = contract_ssm.build(contract_ssm.SHARED, n.REPO, "config", "codeconnection-ref")
GITHUB_REPOSITORY = "FilippoLentoni/FinanceLambdasTool"
BRANCH = "main"
UV_VERSION = "0.12.23"


def _stmts(docs: list[dict[str, Any]]) -> list[iam.PolicyStatement]:
    return [iam.PolicyStatement.from_json(d) for d in docs]


def predeploy_namespace(env: str) -> str:
    return f"PreDeploy{env.capitalize()}"


# ===================================================================== build specs
def _install() -> dict[str, Any]:
    return {"runtime-versions": {"python": "3.12", "nodejs": "22"}, "commands": [f'python3 -m pip install --quiet "uv=={UV_VERSION}"', "uv --version"]}


def build_spec() -> dict[str, Any]:
    return {
        "version": "0.2",
        "env": {"shell": "bash", "variables": {"SOURCE_DATE_EPOCH": "315532800", "UV_LINK_MODE": "copy", "CDK_DISABLE_VERSION_CHECK": "1", "FINPLAN_RELEASE_BUILD": "1"}},
        "phases": {
            "install": _install(),
            "build": {
                "commands": [
                    "uv sync --locked",
                    'uv run python scripts/build_stage.py --out build-output --source-commit "$SOURCE_COMMIT" --rollback-to "$ROLLBACK_TO_RELEASE_ID" --store "$FINPLAN_PIPELINE_STORE"',
                ]
            },
        },
        "artifacts": {"base-directory": "build-output", "files": ["**/*"]},
        "cache": {"paths": ["/root/.cache/uv/**/*", "/root/.npm/**/*"]},
    }


def stage_spec() -> dict[str, Any]:
    """Post-build actions run from BuildOutput only (never the source checkout, never a synth).

    ``predeploy`` writes its resolved values to ``predeploy.env``, which is sourced so CodeBuild
    exports them as the action's variables (``exported-variables``)."""
    exported = sorted(PARAMETERS.values())
    return {
        "version": "0.2",
        "env": {"shell": "bash", "variables": {"UV_LINK_MODE": "copy", **{v: "none" for v in exported}}, "exported-variables": exported},
        "phases": {
            "install": _install(),
            "build": {
                "commands": [
                    "uv sync --locked",
                    'uv run python scripts/stage_runner.py "$FINPLAN_STAGE_ACTION" --env "$FINPLAN_ENV" --release-info release-info.json --pipeline-execution-id "$PIPELINE_EXECUTION_ID" --store "$FINPLAN_PIPELINE_STORE" --variables-file predeploy.env',
                    "if [ -f predeploy.env ]; then set -a; . ./predeploy.env; set +a; fi",
                ]
            },
        },
        "cache": {"paths": ["/root/.cache/uv/**/*"]},
    }


# ===================================================================== helpers
def template_path(stage: cdk.Stage, stack: cdk.Stack) -> str:
    """Path of the stack template inside BuildOutput (``cdk.out/assembly-<Stage>/<file>``)."""
    return f"cdk.out/{stage.artifact_id}/{stack.template_file}"


class PipelineResources:
    def __init__(self, tooling: ToolingStack) -> None:
        self.tooling = tooling
        self.pipeline: codepipeline.Pipeline | None = None
        self.store: s3.IBucket | None = None
        self.roles: dict[str, iam.Role] = {}
        self.projects: dict[str, codebuild.PipelineProject] = {}
        self.log_groups: dict[str, logs.CfnLogGroup] = {}
        self.iac_log_groups = False


def _role(scope: ToolingStack, cid: str, name: str, logical: str, principal: iam.IPrincipal, description: str, statements: list[iam.PolicyStatement] | None = None) -> iam.Role:
    role = iam.Role(scope, cid, role_name=name, assumed_by=principal, description=description)
    for st in statements or []:
        role.add_to_principal_policy(st)
    tag_role(role, logical)
    scope.register_enforced_role(role)
    return role


def _scope_to_environment(role: iam.Role, env: str) -> None:
    """Per-environment pipeline role: ``environment=<env>`` tag and that environment's boundary."""
    Tags.of(role).add("environment", env, priority=200)
    iam.PermissionsBoundary.of(role).apply(ToolingStack.environment_boundary(role, env))


def codebuild_project_names() -> list[str]:
    """Every CodeBuild project of the pipeline (their default log groups are ``/aws/codebuild/<name>``)."""
    return [n.shared_name("pipeline-build-project")] + [n.shared_name("pipeline-build-project", f"{e}-stage") for e in n.ENVIRONMENTS]


def _project_log_group(res: PipelineResources, cid: str, project: codebuild.PipelineProject, name: str) -> None:
    """30-day CodeBuild log group (lesson L6). In IaC only with the ``pipeline-logs`` contract gap
    switched on (:mod:`infra.stacks.contract_gaps`); otherwise the bootstrap applies the retention."""
    if not res.iac_log_groups:
        return
    group = logs.CfnLogGroup(res.tooling, cid, log_group_name=f"/aws/codebuild/{name}", retention_in_days=n.LOG_RETENTION_DAYS)
    group.apply_removal_policy(cdk.RemovalPolicy.DESTROY)
    tag_role(group, "pipeline-build-project")
    group.add_dependency(project.node.default_child)  # type: ignore[arg-type]
    res.log_groups[cid] = group


# ===================================================================== construction
def add_pipeline(tooling: ToolingStack, tool_stacks: Mapping[str, cdk.Stack], *, gaps: frozenset[str] = frozenset()) -> PipelineResources:
    res = PipelineResources(tooling)
    res.iac_log_groups = "pipeline-logs" in gaps
    st = tooling
    owner, repo_name = GITHUB_REPOSITORY.split("/", 1)
    a = Aws.ACCOUNT_ID

    dry_run_passed = CfnParameter(st, "SourceDryRunPassed", type="String", default="false", allowed_values=["false", "true"], description="true once the bootstrap's source-stage dry run fetched main; until then the transition into Build is disabled.")
    dry_run_cond = CfnCondition(st, "SourceDryRunPassedCondition", expression=Fn.condition_equals(dry_run_passed.value_as_string, "true"))

    store = s3.Bucket.from_bucket_name(st, "Store", n.pipeline_store_bucket_name(a))
    res.store = store

    pipeline_role = _role(st, "PipelineRole", n.shared_name("pipeline", "role"), "pipeline-role", iam.ServicePrincipal("codepipeline.amazonaws.com"), "CodePipeline service role of the FinanceLambdasTool pipeline")
    build_role = _role(st, "BuildRole", n.shared_name("pipeline-build-project", "role"), "pipeline-role", iam.ServicePrincipal("codebuild.amazonaws.com"), "Build stage: gates, bundle, synth, asset publishing, release packaging", _stmts(build_role_statements(store.bucket_arn)))
    res.roles.update(pipeline=pipeline_role, build=build_role)

    env_common = {"FINPLAN_PIPELINE_STORE": codebuild.BuildEnvironmentVariable(value=store.bucket_name)}
    image = codebuild.LinuxBuildImage.AMAZON_LINUX_2023_5
    build_project = codebuild.PipelineProject(
        st,
        "BuildProject",
        project_name=n.shared_name("pipeline-build-project"),
        role=build_role,
        environment=codebuild.BuildEnvironment(build_image=image, compute_type=codebuild.ComputeType.SMALL, privileged=False),
        environment_variables=env_common,
        build_spec=codebuild.BuildSpec.from_object(build_spec()),
        timeout=Duration.minutes(30),
        cache=codebuild.Cache.local(codebuild.LocalCacheMode.CUSTOM),
        description="Build stage: gates, arm64 Lambda bundle, cdk synth once, assets, release_id",
    )
    tag_role(build_project, "pipeline-build-project")
    _project_log_group(res, "BuildProjectLogGroup", build_project, n.shared_name("pipeline-build-project"))
    res.projects["build"] = build_project

    source_output = codepipeline.Artifact("SourceOutput")
    build_output = codepipeline.Artifact("BuildOutput")
    connection_arn = ssm.StringParameter.value_for_string_parameter(st, CONNECTION_PARAMETER)
    source = actions.CodeStarConnectionsSourceAction(action_name="Source", owner=owner, repo=repo_name, branch=BRANCH, connection_arn=connection_arn, output=source_output, trigger_on_push=True, variables_namespace="SourceVariables")
    build = actions.CodeBuildAction(
        action_name="BuildAndTest",
        project=build_project,
        input=source_output,
        outputs=[build_output],
        type=actions.CodeBuildActionType.BUILD,
        environment_variables={
            "SOURCE_COMMIT": codebuild.BuildEnvironmentVariable(value=source.variables.commit_id),
            "ROLLBACK_TO_RELEASE_ID": codebuild.BuildEnvironmentVariable(value=f"#{{variables.{ROLLBACK_VARIABLE}}}"),
        },
    )
    pipeline = codepipeline.Pipeline(
        st,
        "Pipeline",
        pipeline_name=n.PIPELINE_NAME,
        pipeline_type=codepipeline.PipelineType.V2,
        artifact_bucket=store,
        role=pipeline_role,
        cross_account_keys=False,
        restart_execution_on_update=False,
        use_pipeline_role_for_actions=True,
        variables=[codepipeline.Variable(variable_name=ROLLBACK_VARIABLE, default_value=NO_ROLLBACK, description="Set to a recorded release_id to redeploy its stored build output without rebuilding (contracts D6).")],
        stages=[codepipeline.StageProps(stage_name=SOURCE_STAGE, actions=[source]), codepipeline.StageProps(stage_name=BUILD_STAGE, actions=[build])],
    )
    tag_role(pipeline, "pipeline")
    res.pipeline = pipeline

    for env in n.ENVIRONMENTS:
        if env == "prod":
            pipeline.add_stage(stage_name="Approval", actions=[actions.ManualApprovalAction(action_name="ApproveProd", additional_information="Approve promotion of this FinanceLambdasTool release to prod after the gamma suite passed. The approver and time are recorded in the prod release manifest.")])
        _add_env_stage(res, env, tool_stacks[env], build_output, env_common, image)

    cfn = pipeline.node.default_child
    assert isinstance(cfn, codepipeline.CfnPipeline)
    cfn.add_property_override("DisableInboundStageTransitions", Fn.condition_if(dry_run_cond.logical_id, Aws.NO_VALUE, [{"StageName": BUILD_STAGE, "Reason": "finplan bootstrap: the source-stage dry run has not passed yet"}]))
    cdk.CfnOutput(st, "PipelineName", value=n.PIPELINE_NAME)
    return res


def _add_env_stage(res: PipelineResources, env: str, tools_stack: cdk.Stack, build_output: codepipeline.Artifact, env_common: dict[str, codebuild.BuildEnvironmentVariable], image: codebuild.IBuildImage) -> None:
    st = res.tooling
    cap = env.capitalize()
    assert res.store is not None and res.pipeline is not None
    p, r, a = Aws.PARTITION, Aws.REGION, Aws.ACCOUNT_ID
    deploy_role = _role(st, f"DeployRole{cap}", n.deploy_role_name(env), "deploy-role", iam.ArnPrincipal(res.roles["pipeline"].role_arn), f"Scoped {env} deploy action role (CodePipeline assumes it)")
    exec_role = _role(st, f"DeployExecRole{cap}", n.exec_role_name(env), "deploy-role", iam.ServicePrincipal("cloudformation.amazonaws.com"), f"CloudFormation execution role for the {env} FinanceLambdasTool stack", _stmts(deploy_execution_statements(env, res.store.bucket_arn, partition=p, region=r, account=a)))
    stage_role = _role(st, f"StageRole{cap}", n.stage_role_name(env), "pipeline-role", iam.ServicePrincipal("codebuild.amazonaws.com"), f"{env} pre-deploy resolver, release publisher and test runner", _stmts(stage_role_statements(env, res.store.bucket_arn, partition=p, region=r, account=a)))
    for role in (deploy_role, exec_role, stage_role):
        _scope_to_environment(role, env)
    res.roles.update({f"deploy-{env}": deploy_role, f"exec-{env}": exec_role, f"stage-{env}": stage_role})

    project_name = n.shared_name("pipeline-build-project", f"{env}-stage")
    project = codebuild.PipelineProject(
        st,
        f"StageProject{cap}",
        project_name=project_name,
        role=stage_role,
        environment=codebuild.BuildEnvironment(build_image=image, compute_type=codebuild.ComputeType.SMALL, privileged=False),
        environment_variables={**env_common, "FINPLAN_ENV": codebuild.BuildEnvironmentVariable(value=env)},
        build_spec=codebuild.BuildSpec.from_object(stage_spec()),
        timeout=Duration.minutes(20),
        cache=codebuild.Cache.local(codebuild.LocalCacheMode.CUSTOM),
        description=f"{env}: pre-deploy checks and reference resolution, release publishing and the {ENV_SUITES[env]} suite",
    )
    tag_role(project, "pipeline-build-project")
    _project_log_group(res, f"StageProject{cap}LogGroup", project, project_name)
    res.projects[env] = project

    stage = cdk.Stage.of(tools_stack)
    if stage is None:  # pragma: no cover - add_to_app always creates a Stage
        raise ValueError("environment stacks must live in a cdk.Stage")
    execution_id = codebuild.BuildEnvironmentVariable(value="#{codepipeline.PipelineExecutionId}")

    def runner(name: str, action: str, order: int, kind: actions.CodeBuildActionType, namespace: str | None = None) -> actions.CodeBuildAction:
        return actions.CodeBuildAction(
            action_name=name,
            project=project,
            input=build_output,
            type=kind,
            run_order=order,
            variables_namespace=namespace,
            environment_variables={"FINPLAN_STAGE_ACTION": codebuild.BuildEnvironmentVariable(value=action), "PIPELINE_EXECUTION_ID": execution_id},
        )

    predeploy = runner("PreDeploy", "predeploy", 1, actions.CodeBuildActionType.TEST, predeploy_namespace(env))
    deploy = actions.CloudFormationCreateUpdateStackAction(
        action_name="DeployTools",
        stack_name=tools_stack.stack_name,
        template_path=build_output.at_path(template_path(stage, tools_stack)),
        parameter_overrides={param: predeploy.variable(var) for param, var in PARAMETERS.items()},
        admin_permissions=False,
        role=deploy_role,
        deployment_role=exec_role,
        cfn_capabilities=[cdk.CfnCapabilities.NAMED_IAM, cdk.CfnCapabilities.AUTO_EXPAND],
        replace_on_failure=False,
        run_order=2,
    )
    publish = runner("PublishRelease", "publish", 3, actions.CodeBuildActionType.BUILD)
    suite = ENV_SUITES[env]
    tests = runner("".join(part.capitalize() for part in suite.split("-")) + "Tests", "tests", 4, actions.CodeBuildActionType.TEST)
    res.pipeline.add_stage(stage_name=cap, actions=[predeploy, deploy, publish, tests])


def add_pipeline_if_complete(tooling: ToolingStack, tool_stacks: Mapping[str, cdk.Stack]) -> PipelineResources | None:
    """The pipeline needs all three environment stacks (``-c envs=...`` selecting fewer skips it)."""
    missing = [e for e in n.ENVIRONMENTS if e not in tool_stacks]
    if missing:
        Annotations.of(tooling).add_warning_v2("finplan:pipeline-skipped", f"pipeline not synthesized: environments {missing} are not selected (-c envs=...)")
        return None
    from .contract_gaps import CONTEXT_KEY, enabled_gaps

    return add_pipeline(tooling, tool_stacks, gaps=enabled_gaps(tooling.node.try_get_context(CONTEXT_KEY)))
