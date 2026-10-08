"""Pipeline structure (task 9.3; REL-05 / ENV-09) and the account-level stacks (task 9.6; lesson L1)."""

from __future__ import annotations

import copy
import json

import pytest
from finplan_contracts.bootstrap import check_deploy_roles
from finplan_contracts.pipeline_check import check_pipeline_template

from infra.stacks import naming as n
from infra.stacks.pipeline import ENV_SUITES, codebuild_project_names, predeploy_namespace, stage_spec
from infra.stacks.tools import PARAMETERS
from tests.unit.infra_support import ENVS, assembly, needs_node, resources, tags, templates

pytestmark = [pytest.mark.synth, needs_node]


@pytest.fixture(scope="module")
def tooling():
    return templates(assembly())["tooling"]


def _pipeline(t):
    return next(iter(resources(t, "AWS::CodePipeline::Pipeline").values()))["Properties"]


def test_pipeline_follows_the_contract_standard(tooling):
    """REL-05: Source -> Build -> Beta -> Gamma -> Approval -> Prod, V2, rollback variable,
    artifact-only promotion, scoped deploy roles, tests after every deploy."""
    assert check_pipeline_template(tooling) == []
    assert check_deploy_roles(tooling) == []
    p = _pipeline(tooling)
    assert [s["Name"] for s in p["Stages"]] == ["Source", "Build", "Beta", "Gamma", "Approval", "Prod"]
    assert p["PipelineType"] == "V2"
    assert {"Name": "rollback_to_release_id", "DefaultValue": "none", "Description": p["Variables"][0]["Description"]} == p["Variables"][0]
    src = p["Stages"][0]["Actions"][0]["Configuration"]
    assert src["FullRepositoryId"] == "FilippoLentoni/FinanceLambdasTool" and src["BranchName"] == "main"
    assert "/finplan/shared/financelambdastool/config/codeconnection-ref" in json.dumps(tooling["Parameters"])


def test_environment_stage_actions(tooling):
    p = _pipeline(tooling)
    for stage in p["Stages"][2:]:
        if stage["Name"] == "Approval":
            assert [a["ActionTypeId"]["Provider"] for a in stage["Actions"]] == ["Manual"]
            continue
        env = stage["Name"].lower()
        names = [(a["Name"], a["RunOrder"]) for a in stage["Actions"]]
        suite = "".join(x.capitalize() for x in ENV_SUITES[env].split("-")) + "Tests"
        assert names == [("PreDeploy", 1), ("DeployTools", 2), ("PublishRelease", 3), (suite, 4)]
        pre, deploy = stage["Actions"][0], stage["Actions"][1]
        assert pre["Namespace"] == predeploy_namespace(env)
        cfg = deploy["Configuration"]
        assert cfg["StackName"] == n.stack_name(env)
        overrides = json.loads(cfg["ParameterOverrides"])
        assert overrides == {param: f"#{{{predeploy_namespace(env)}.{var}}}" for param, var in PARAMETERS.items()}
        assert cfg["TemplatePath"].startswith("BuildOutput::cdk.out/assembly-")
        assert [i["Name"] for a in stage["Actions"] for i in a.get("InputArtifacts", [])] == ["BuildOutput"] * 4


def test_promotion_is_artifact_only_and_never_resynthesizes(tooling):
    assert "cdk synth" not in json.dumps(stage_spec())
    spec = stage_spec()
    assert sorted(spec["env"]["exported-variables"]) == sorted(PARAMETERS.values())
    broken = copy.deepcopy(tooling)
    p = _pipeline(broken)
    p["Stages"][3]["Actions"][0]["InputArtifacts"] = [{"Name": "SourceOutput"}]
    assert any("source checkout" in str(f) for f in check_pipeline_template(broken))


def test_build_is_gated_until_the_source_dry_run(tooling):
    p = _pipeline(tooling)
    assert "SourceDryRunPassedCondition" in json.dumps(p["DisableInboundStageTransitions"])
    assert tooling["Parameters"]["SourceDryRunPassed"]["Default"] == "false"


def test_pipeline_roles_are_scoped(tooling):
    roles = resources(tooling, "AWS::IAM::Role")
    by_name = {r["Properties"]["RoleName"]: r for r in roles.values()}
    for env in ENVS:
        for name in (n.deploy_role_name(env), n.exec_role_name(env), n.stage_role_name(env)):
            r = by_name[name]
            assert tags(r)["environment"] == env
            assert f"finplan-{env}-permission-boundary" in json.dumps(r["Properties"]["PermissionsBoundary"])
    for name in (n.shared_name("pipeline", "role"), n.shared_name("pipeline-build-project", "role")):
        r = by_name[name]
        assert tags(r)["environment"] == "shared"
        assert "finplan-shared-permission-boundary" in json.dumps(r["Properties"]["PermissionsBoundary"])


def test_codebuild_projects_and_log_group_names(tooling):
    projects = {p["Properties"]["Name"] for p in resources(tooling, "AWS::CodeBuild::Project").values()}
    assert projects == set(codebuild_project_names())
    for p in resources(tooling, "AWS::CodeBuild::Project").values():
        assert p["Properties"]["Environment"]["PrivilegedMode"] is False
        assert "LogsConfig" not in p["Properties"] or "GroupName" not in json.dumps(p["Properties"]["LogsConfig"])


def test_store_bucket(asm=None):
    t = templates(assembly())["store"]
    bucket = next(iter(resources(t, "AWS::S3::Bucket").values()))
    assert bucket["DeletionPolicy"] == "Retain"
    assert bucket["Properties"]["VersioningConfiguration"]["Status"] == "Enabled"
    assert tags(bucket)["logical-role"] == "pipeline-artifact-bucket" and tags(bucket)["environment"] == "shared"


def test_no_dynamodb_table_policy_or_stream_actions():
    """Lesson L2 regression: no table resource policy may list stream actions (none declared here)."""
    text = json.dumps(templates(assembly()))
    for action in ("dynamodb:GetRecords", "dynamodb:GetShardIterator", "dynamodb:DescribeStream", "dynamodb:ListStreams"):
        assert action not in text
    assert '"AWS::DynamoDB::Table"' not in text
