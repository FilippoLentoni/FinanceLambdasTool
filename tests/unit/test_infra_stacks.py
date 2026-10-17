"""Synth tests of the tool stacks (tasks 8.1-8.3; ENVW-01..ENVW-04, ENVW-07, ENVW-08) and the
real-deploy lessons L1, L3, L6 on the synthesized templates. Offline: no lookup, no account."""

from __future__ import annotations

import copy
import json

import pytest

from finplan_tools.core.config import DEFAULT_TOOL_LIMITS
from finplan_tools.core.registry import CATALOG, ROLE_CLASSES
from infra.stacks import naming as n
from tests.unit.infra_support import ENVS, assembly, needs_node, resources, tags, templates

pytestmark = [pytest.mark.synth, needs_node]


@pytest.fixture(scope="module")
def asm():
    return assembly()


@pytest.fixture(scope="module")
def tpl(asm):
    return templates(asm)


# ===================================================================== L1: no CDK bootstrap
def test_no_cdk_bootstrap_reference_anywhere(asm):
    """Lesson L1: no cdk-hnb659fds role, no cdk-*-assets bucket, no bootstrap-version rule."""
    from scripts.infra_gates import gate_cdk_bootstrap_free

    class Ctx:
        assembly = asm

        def note(self, *_a):
            pass

    assert gate_cdk_bootstrap_free(Ctx()) == []
    for p in asm.rglob("*.json"):
        if p.name == "tree.json":
            continue
        text = p.read_text()
        assert "cdk-hnb659fds" not in text and "BootstrapVersion" not in text, p.name


def test_store_is_legacy_inline_and_tooling_stages_in_the_store(asm, tpl):
    manifest = json.loads((asm / "manifest.json").read_text())
    store = next(a for a in manifest["artifacts"].values() if (a.get("properties") or {}).get("stackName") == "finplan-shared-financelambdastool-pipeline-store")
    assert "assumeRoleArn" not in json.dumps(store) and "Parameters" not in tpl["store"]
    assets = json.loads(next(asm.glob("Tooling.assets.json")).read_text())
    for asset in assets["files"].values():
        for dest in asset["destinations"].values():
            assert dest["bucketName"] == "finplan-shared-financelambdastool-pipeline-store-${AWS::AccountId}"
            assert dest["objectKey"].startswith("bootstrap/")
            assert "assumeRoleArn" not in dest


def test_environment_code_asset_lives_in_the_store(tpl):
    for env in ENVS:
        for fn in resources(tpl[env], "AWS::Lambda::Function").values():
            code = fn["Properties"]["Code"]
            assert "finplan-shared-financelambdastool-pipeline-store-" in json.dumps(code["S3Bucket"])
            assert code["S3Key"].startswith("assets/")


# ===================================================================== 8.1 functions and roles
@pytest.mark.parametrize("env", ENVS)
def test_one_arm64_function_per_tool_with_alias_and_log_group(tpl, env):
    t = tpl[env]
    fns = resources(t, "AWS::Lambda::Function")
    assert len(fns) == len(CATALOG)
    by_tool = {f["Properties"]["Environment"]["Variables"]["FINPLAN_TOOL_NAME"]: (lid, f) for lid, f in fns.items()}
    assert set(by_tool) == set(CATALOG)
    code_keys = {json.dumps(f["Properties"]["Code"], sort_keys=True) for f in fns.values()}
    assert len(code_keys) == 1  # one artifact for every tool (ENV-10)
    aliases = resources(t, "AWS::Lambda::Alias")
    groups = resources(t, "AWS::Logs::LogGroup")
    for tool, (lid, f) in by_tool.items():
        p = f["Properties"]
        assert p["FunctionName"] == n.function_name(env, tool)
        assert p["Architectures"] == ["arm64"] and p["Runtime"] == "python3.12" and p["MemorySize"] == 256
        assert p["Handler"] == "finplan_tools.handler.handler"
        assert "VpcConfig" not in p and "ReservedConcurrentExecutions" not in p
        assert p["Timeout"] == DEFAULT_TOOL_LIMITS["timeouts_seconds"][CATALOG[tool].timeout_key]
        assert set(p["Environment"]["Variables"]) == {"FINPLAN_ENV", "FINPLAN_TOOL_NAME", "FINPLAN_RELEASE_ID", "FINPLAN_ACCOUNT_ID"}
        assert p["Environment"]["Variables"]["FINPLAN_ENV"] == env
        assert tags(f)["logical-role"] == "tool-lambda"
        assert [a for a in aliases.values() if a["Properties"]["FunctionName"] == {"Ref": lid} and a["Properties"]["Name"] == "current"]
        # lesson L6: an explicit 30-day group named after the function
        assert [g for g in groups.values() if g["Properties"]["RetentionInDays"] == 30 and {"Ref": lid} in g["Properties"]["LogGroupName"]["Fn::Join"][1]]


@pytest.mark.parametrize("env", ENVS)
def test_three_role_classes_with_boundary_and_own_log_streams(tpl, env):
    """D1 role classes; ENVW-02 boundary; lesson L3: explicit roles write their own log streams."""
    t = tpl[env]
    roles = resources(t, "AWS::IAM::Role")
    names = {r["Properties"]["RoleName"]: (lid, r) for lid, r in roles.items()}
    assert set(names) == {n.role_class_role_name(env, c) for c in ROLE_CLASSES}
    fns = resources(t, "AWS::Lambda::Function")
    for cls in ROLE_CLASSES:
        lid, role = names[n.role_class_role_name(env, cls)]
        boundary = json.dumps(role["Properties"]["PermissionsBoundary"])
        assert f"policy/finplan-{env}-permission-boundary" in boundary
        assert tags(role)["logical-role"] == f"tool-role-{cls}"
        doc = json.dumps(role["Properties"]["Policies"])
        for tool, e in CATALOG.items():
            group = f"/aws/lambda/{n.function_name(env, tool)}"
            assert (group in doc) == (e.role_class == cls)
        assert "logs:CreateLogStream" in doc and "logs:PutLogEvents" in doc
        for f in fns.values():
            if f["Properties"]["Environment"]["Variables"]["FINPLAN_TOOL_NAME"] in [t for t, e in CATALOG.items() if e.role_class == cls]:
                assert f["Properties"]["Role"] == {"Fn::GetAtt": [lid, "Arn"]}


def test_missing_boundary_fails_the_policy_check(tpl):
    """ENVW-02 negative: a role class without the environment boundary fails the build check."""
    from finplan_contracts.boundaries import check_role_boundaries

    assert check_role_boundaries(tpl["gamma"]) == []
    broken = copy.deepcopy(tpl["gamma"])
    lid = next(iter(resources(broken, "AWS::IAM::Role")))
    del broken["Resources"][lid]["Properties"]["PermissionsBoundary"]
    findings = check_role_boundaries(broken)
    assert findings and lid in str(findings[0])


# ===================================================================== 8.2 same-environment wiring
@pytest.mark.parametrize("env", ENVS)
def test_references_only_own_environment(tpl, env):
    """ENVW-01: the gamma template names only /finplan/gamma/... (other environments appear only in
    explicit denies); no literal endpoint, ARN or account."""
    from scripts.infra_gates import wiring_problems

    assert wiring_problems(tpl[env], env, env) == []
    allows = json.dumps([s for r in resources(tpl[env], "AWS::IAM::Role").values() for p in r["Properties"]["Policies"] for s in p["PolicyDocument"]["Statement"] if s["Effect"] == "Allow"])
    assert f"/finplan/{env}/" in allows
    for other in ENVS:
        if other != env:
            assert f"/finplan/{other}" not in allows and f"finplan-{other}-" not in allows


def test_wiring_check_catches_a_cross_environment_reference(tpl):
    from scripts.infra_gates import wiring_problems

    bad = copy.deepcopy(tpl["gamma"])
    fn = next(iter(resources(bad, "AWS::Lambda::Function")))
    bad["Resources"][fn]["Properties"]["Environment"]["Variables"]["X"] = "/finplan/prod/financialplanning/api/plan-endpoint"
    assert wiring_problems(bad, "gamma", "gamma")
    literal = copy.deepcopy(tpl["gamma"])
    literal["Resources"][fn]["Properties"]["Description"] = "https://" + "abcdefghij" + ".execute-api." + "us-east-2.amazonaws.com/live"
    assert wiring_problems(literal, "gamma", "gamma")


@pytest.mark.parametrize("env", ENVS)
def test_producer_api_ids_are_parameters(tpl, env):
    params = tpl[env]["Parameters"]
    assert "Default" not in params["PlanApiId"] and "Default" not in params["IngestionApiId"]
    assert params["JobApiId"]["Default"] == "none" and params["DirectTestPrincipal"]["Default"] == "none" and params["GatewayPrincipalRoleName"]["Default"] == "none"
    assert params["GatewayPrincipalRoleName"]["AllowedPattern"] == f"^(none|finplan-{env}-financeagent-[A-Za-z0-9+=,.@_-]{{1,40}})$"
    import re

    gw = re.compile(params["GatewayPrincipalRoleName"]["AllowedPattern"])
    assert gw.match(f"finplan-{env}-financeagent-gateway-role")
    for other in ENVS:
        if other != env:
            assert not gw.match(f"finplan-{other}-financeagent-gateway-role")  # gamma -> gamma only
    dt = re.compile(params["DirectTestPrincipal"]["AllowedPattern"])
    assert dt.match("role/owner-role") and dt.match("user/owner") and not dt.match(":".join(["arn", "aws", "iam", "", "0" * 12, "role/x"])) and not dt.match("role/*")


# ===================================================================== 8.3 invoke grants
def _grants(t):
    fns = resources(t, "AWS::Lambda::Function")
    aliases = resources(t, "AWS::Lambda::Alias")
    tool_of_alias = {lid: fns[a["Properties"]["FunctionName"]["Ref"]]["Properties"]["Environment"]["Variables"]["FINPLAN_TOOL_NAME"] for lid, a in aliases.items()}
    out: dict[str, set[str]] = {tool: set() for tool in CATALOG}
    for p in resources(t, "AWS::Lambda::Permission").values():
        props = p["Properties"]
        alias = props["FunctionName"]["Ref"]
        principal = json.dumps(props["Principal"])
        kind = "stage" if "pipeline-stage-role" in principal else "direct" if "DirectTestPrincipal" in principal else "gateway" if "GatewayPrincipalRoleName" in principal else "?"
        if kind in ("direct", "gateway"):
            assert p["Condition"] == {"direct": "HasDirectTestPrincipal", "gateway": "HasGatewayPrincipal"}[kind]
        out[tool_of_alias[alias]].add(kind)
    return out


@pytest.mark.parametrize("env", ("beta", "gamma"))
def test_beta_gamma_grants(tpl, env):
    """ENVW-03/ENVW-04: every tool to the stage role, the direct-test principal and the Gateway role, on the alias."""
    from scripts.infra_gates import grant_problems

    assert grant_problems(tpl[env], env, env) == []
    assert all(g == {"stage", "direct", "gateway"} for g in _grants(tpl[env]).values())


def test_prod_direct_and_smoke_grants_are_read_only(tpl):
    """ENVW-04: prod direct-test principal and smoke role reach read-only tools only."""
    from scripts.infra_gates import grant_problems

    assert grant_problems(tpl["prod"], "prod", "prod") == []
    grants = _grants(tpl["prod"])
    for tool, e in CATALOG.items():
        assert grants[tool] == ({"gateway"} if e.state_changing else {"stage", "direct", "gateway"}), tool


def test_conditions_make_absent_parameters_grant_nothing(tpl):
    """ENVW-08 / 'Gateway not yet published': the grants exist only when the parameter is not none."""
    cond = tpl["gamma"]["Conditions"]
    assert cond["HasDirectTestPrincipal"] == {"Fn::Not": [{"Fn::Equals": [{"Ref": "DirectTestPrincipal"}, "none"]}]}
    assert cond["HasGatewayPrincipal"] == {"Fn::Not": [{"Fn::Equals": [{"Ref": "GatewayPrincipalRoleName"}, "none"]}]}


def test_grant_check_rejects_wildcards_and_prod_writes(tpl):
    from scripts.infra_gates import grant_problems

    bad = copy.deepcopy(tpl["prod"])
    perm = next(iter(resources(bad, "AWS::Lambda::Permission")))
    bad["Resources"][perm]["Properties"]["Principal"] = "*"
    assert any("wildcard" in p or "unexpected" in p for p in grant_problems(bad, "prod", "prod"))
    # a direct-test grant on a prod write tool
    bad2 = copy.deepcopy(tpl["prod"])
    aliases = resources(bad2, "AWS::Lambda::Alias")
    fns = resources(bad2, "AWS::Lambda::Function")
    write_alias = next(lid for lid, a in aliases.items() if CATALOG[fns[a["Properties"]["FunctionName"]["Ref"]]["Properties"]["Environment"]["Variables"]["FINPLAN_TOOL_NAME"]].state_changing)
    bad2["Resources"]["Injected"] = {"Type": "AWS::Lambda::Permission", "Condition": "HasDirectTestPrincipal", "Properties": {"Action": "lambda:InvokeFunction", "FunctionName": {"Ref": write_alias}, "Principal": {"Fn::Sub": ["x", {"Principal": {"Ref": "DirectTestPrincipal"}}]}}}
    assert any("state-changing" in p for p in grant_problems(bad2, "prod", "prod"))


# ===================================================================== ENVW-07 cost, ownership
def test_cost_check_passes_and_catches_provisioned_concurrency(tpl):
    from scripts.infra_gates import cost_problems

    for name, t in tpl.items():
        assert cost_problems(t, name) == [], name
    bad = copy.deepcopy(tpl["beta"])
    version = next(iter(resources(bad, "AWS::Lambda::Version")))
    bad["Resources"][version]["Properties"]["ProvisionedConcurrencyConfig"] = {"ProvisionedConcurrentExecutions": 1}
    bad["Resources"]["Nat"] = {"Type": "AWS::EC2::NatGateway", "Properties": {}}
    problems = cost_problems(bad, "beta")
    assert any("provisioned concurrency" in p for p in problems) and any("NatGateway" in p for p in problems)


def test_ownership_and_shared_checks_pass(tpl):
    from finplan_contracts.boundaries import check_shared_resources
    from finplan_contracts.ownership import check_template

    for name, t in tpl.items():
        report = check_template(t, "financelambdastool", name=name)
        assert report.ok, [str(p) for p in report.problems]
        assert check_shared_resources(t, repo="financelambdastool") == []


def test_no_budget_boundary_or_connection_declared(tpl):
    """Lesson L6: FinancialPlanning owns the budget and boundaries; the CodeConnection is reused."""
    text = json.dumps(tpl)
    for rtype in ("AWS::Budgets::Budget", "AWS::Budgets::BudgetsAction", "AWS::IAM::ManagedPolicy", "AWS::CodeStarConnections::Connection", "AWS::CodeConnections::Connection"):
        assert f'"{rtype}"' not in text


def test_live_permission_scan_passes_on_templates(asm):
    from finplan_contracts import live_perms

    from scripts.infra_gates import templates_of

    _, findings = live_perms.scan_paths(templates_of(asm))
    assert findings == []


def test_log_retention_gate(asm, tpl):
    from infra.stacks.pipeline import codebuild_project_names
    from scripts.infra_gates import log_retention_problems

    for name, t in tpl.items():
        assert log_retention_problems(t, name, bootstrap_managed=codebuild_project_names()) == []
    bad = copy.deepcopy(tpl["beta"])
    group = next(iter(resources(bad, "AWS::Logs::LogGroup")))
    bad["Resources"][group]["Properties"]["RetentionInDays"] = 3653
    assert log_retention_problems(bad, "beta")
    assert log_retention_problems(tpl["tooling"], "tooling")  # without the bootstrap list the CodeBuild groups are missing


def test_pipeline_logs_gap_declares_tagged_codebuild_groups():
    """Contract gap pipeline-logs: once the matrix row lists AWS::Logs::LogGroup the groups move to IaC."""
    t = templates(assembly(json.dumps({"finplan:contract-gaps": "pipeline-logs"})))["tooling"]
    groups = resources(t, "AWS::Logs::LogGroup")
    names = {g["Properties"]["LogGroupName"] for g in groups.values()}
    from infra.stacks.pipeline import codebuild_project_names

    assert names == {f"/aws/codebuild/{p}" for p in codebuild_project_names()}
    assert all(g["Properties"]["RetentionInDays"] == 30 and tags(g)["logical-role"] == "pipeline-build-project" for g in groups.values())


# ===================================================================== L3 source-only code
def test_release_mode_refuses_source_only_code(tmp_path):
    from infra.stacks.lambda_code import SourceOnlyCodeError, function_code

    with pytest.raises(SourceOnlyCodeError):
        function_code({"FINPLAN_RELEASE_BUILD": "1"})
    with pytest.raises(SourceOnlyCodeError):
        function_code({"CODEBUILD_BUILD_ID": "x"})
    (tmp_path / "finplan_tools").mkdir()
    with pytest.raises(SourceOnlyCodeError, match="incomplete"):
        function_code({"FINPLAN_RELEASE_BUILD": "1", "FINPLAN_LAMBDA_BUNDLE_DIR": str(tmp_path)})
    function_code({"FINPLAN_RELEASE_BUILD": "0"})  # local synth packages src/ (not deployable)


def test_names_fit_iam_and_lambda_limits():
    for env in ENVS:
        for tool in CATALOG:
            assert len(n.function_name(env, tool)) <= 64
        for cls in ROLE_CLASSES:
            name = n.role_class_role_name(env, cls)
            assert name.startswith(f"finplan-{env}-financelambdastool-tool-role-{cls}")  # the platform's grant pattern
        for name in (n.deploy_role_name(env), n.exec_role_name(env), n.stage_role_name(env)):
            assert len(name) <= 64
    assert len(n.pipeline_store_bucket_name("0" * 12)) <= 63
