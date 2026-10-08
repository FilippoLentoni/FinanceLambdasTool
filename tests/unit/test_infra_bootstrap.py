"""One-time bootstrap (task 9.6; REL-09, REL-10; lessons L1, L6) with mocked STS, SSM, CodeConnections,
CodePipeline, Price List and CloudWatch Logs clients. Nothing here calls AWS."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from finplan_contracts.bootstrap import ROOT_RECOMMENDATION, BootstrapConfig, BootstrapStop, Clients

from finplan_tools.core.config import DEFAULT_TOOL_LIMITS
from scripts import bootstrap
from tests.unit.infra_support import ACCOUNT, REGION, assembly, needs_node

CONNECTION = ":".join(["arn", "aws", "codeconnections", REGION, ACCOUNT, "connection/test"])


class ParameterNotFound(Exception):
    pass


class Recorder:
    def __init__(self) -> None:
        self.events: list[str] = []


class FakeSsm:
    def __init__(self, rec: Recorder, values: dict[str, str] | None = None) -> None:
        self.rec = rec
        self.values = dict(values or {})

    def get_parameter(self, Name):
        if Name not in self.values:
            raise ParameterNotFound(Name)
        return {"Parameter": {"Value": self.values[Name]}}

    def put_parameter(self, Name, Value, Overwrite=False, **_kw):
        if Name in self.values and not Overwrite:
            raise RuntimeError("ParameterAlreadyExists")
        self.rec.events.append(f"ssm:put:{Name}")
        self.values[Name] = Value


class FakeLogs:
    def __init__(self, rec: Recorder) -> None:
        self.rec = rec
        self.retention: dict[str, int] = {}
        self.tags: dict[str, dict] = {}

    def create_log_group(self, logGroupName, tags):
        self.rec.events.append(f"logs:create:{logGroupName}")
        self.tags[logGroupName] = tags

    def put_retention_policy(self, logGroupName, retentionInDays):
        self.retention[logGroupName] = retentionInDays


class FakePipeline:
    def __init__(self, rec: Recorder, status: str = "Succeeded") -> None:
        self.rec = rec
        self.status = status
        self.enabled = False

    def disable_stage_transition(self, **kw):
        self.rec.events.append("pipeline:disable")

    def enable_stage_transition(self, **kw):
        self.enabled = True
        self.rec.events.append("pipeline:enable")

    def start_pipeline_execution(self, name):
        return {"pipelineExecutionId": "exec-1"}

    def get_pipeline_state(self, name):
        return {"stageStates": [{"stageName": "Source", "latestExecution": {"pipelineExecutionId": "exec-1", "status": self.status}}]}


def clients(rec: Recorder, *, root: bool = False, status: str = "Succeeded", ssm_values: dict[str, str] | None = None) -> tuple[Clients, FakeLogs, FakePipeline]:
    caller = ":".join(["arn", "aws", "iam", "", ACCOUNT, "root" if root else "user/operator"])
    sts = SimpleNamespace(get_caller_identity=lambda: {"Account": ACCOUNT, "Arn": caller})
    conn = SimpleNamespace(get_connection=lambda ConnectionArn: {"Connection": {"ConnectionStatus": "AVAILABLE"}})
    pricing = SimpleNamespace(get_products=lambda **kw: {"PriceList": []})
    pipe = FakePipeline(rec, status)
    return Clients(sts=sts, codeconnections=conn, ssm=FakeSsm(rec, ssm_values), codepipeline=pipe, pricing=pricing), FakeLogs(rec), pipe


def config() -> BootstrapConfig:
    return bootstrap.load_bootstrap_config({"account_id": ACCOUNT, "primary_region": REGION, "codeconnection_arn": CONNECTION}, FakeSsm(Recorder()))


def ok_runner(rec: Recorder):
    def run(cmd, cwd, env):
        rec.events.append("cdk:deploy")
        return SimpleNamespace(returncode=0)

    return run


@pytest.fixture(scope="module")
def asm():
    return assembly()


# ===================================================================== assembly (L1)
@needs_node
def test_bootstrap_assembly_keeps_only_the_tooling_stacks(asm, tmp_path):
    out = bootstrap.bootstrap_assembly(asm, tmp_path / "boot")
    manifest = json.loads((out / "manifest.json").read_text())
    names = sorted((a.get("properties") or {}).get("stackName") for a in manifest["artifacts"].values() if a["type"] == "aws:cloudformation:stack")
    assert names == ["finplan-shared-financelambdastool-pipeline-store", "finplan-shared-financelambdastool-tooling"]
    assert bootstrap.cdk_bootstrap_references(out) == []


def test_bootstrap_assembly_refuses_cdk_bootstrap_references(tmp_path):
    src = tmp_path / "asm"
    src.mkdir()
    tpl = {"Resources": {"X": {"Type": "AWS::S3::Bucket", "Properties": {"BucketName": "cdk-" + "hnb659fds" + "-assets-x"}}}}
    (src / "S.template.json").write_text(json.dumps(tpl))
    arts = {f"S{i}": {"type": "aws:cloudformation:stack", "properties": {"stackName": s, "templateFile": "S.template.json"}} for i, s in enumerate(bootstrap.BOOTSTRAP_STACKS)}
    (src / "manifest.json").write_text(json.dumps({"version": "x", "artifacts": arts}))
    with pytest.raises(BootstrapStop, match="CDKToolkit"):
        bootstrap.bootstrap_assembly(src, tmp_path / "out")
    with pytest.raises(BootstrapStop, match="not synthesized"):
        bootstrap.bootstrap_assembly(tmp_path / "missing", tmp_path / "out2")


# ===================================================================== full sequence (REL-09, REL-10)
@needs_node
def test_full_bootstrap_prints_plan_before_any_change_and_accepts_root(asm, tmp_path):
    rec = Recorder()
    cl, logs, pipe = clients(rec, root=True)
    printed: list[str] = []

    def out(msg: str) -> None:
        printed.append(msg)
        rec.events.append("print")

    approvals = []

    def approve(plan):
        approvals.append(plan)
        rec.events.append("approved")
        return True

    report = bootstrap.run(config(), cl, logs=logs, session_region=REGION, direct_test={"beta": "owner-operator", "gamma": "owner-operator"}, assembly=asm, bootstrap_dir=tmp_path / "boot", approve=approve, runner=ok_runner(rec), record_path=tmp_path / "dry.json", sleep=lambda _s: None, out=out)
    assert report.completed and report.caller_is_root
    text = "\n".join(printed)
    assert "Stacks to deploy (exact):" in text and "Cost estimate" in text  # REL-10
    assert ROOT_RECOMMENDATION in text
    first_change = next(i for i, e in enumerate(rec.events) if e.startswith(("ssm:put", "cdk:", "logs:")))
    assert rec.events.index("approved") < first_change and rec.events.index("print") < first_change
    ssm = cl.ssm.values
    assert ssm["/finplan/shared/financelambdastool/config/codeconnection-ref"] == CONNECTION
    for env in ("beta", "gamma", "prod"):
        assert json.loads(ssm[f"/finplan/{env}/financelambdastool/config/tool-limits"]) == DEFAULT_TOOL_LIMITS
    assert ssm["/finplan/beta/financelambdastool/config/direct-test-principal-name"] == "owner-operator"
    assert "/finplan/prod/financelambdastool/config/direct-test-principal-name" not in ssm  # absent -> no grant
    assert ssm["/finplan/shared/financelambdastool/config/budget-enforced-role-names"] == "finplan-shared-financelambdastool-pipeline-role,finplan-shared-financelambdastool-pipeline-build-project-role"
    assert set(logs.retention.values()) == {30} and len(logs.retention) == 4  # lesson L6
    assert all(t["logical-role"] == "pipeline-build-project" and t["environment"] == "shared" for t in logs.tags.values())
    assert pipe.enabled and json.loads((tmp_path / "dry.json").read_text())["deploy_stages_enabled"] is True
    assert rec.events.index("logs:create:/aws/codebuild/finplan-shared-financelambdastool-pipeline-build-project") < rec.events.index("pipeline:enable")


@needs_node
def test_declined_approval_changes_nothing(asm, tmp_path):
    rec = Recorder()
    cl, logs, _ = clients(rec)
    with pytest.raises(BootstrapStop, match="did not confirm"):
        bootstrap.run(config(), cl, logs=logs, session_region=REGION, assembly=asm, bootstrap_dir=tmp_path / "boot", approve=lambda p: False, runner=ok_runner(rec), out=lambda _m: None)
    assert not [e for e in rec.events if e.startswith(("ssm:put", "cdk:", "logs:"))]


@needs_node
def test_dry_run_failure_keeps_deploy_stages_disabled(asm, tmp_path):
    rec = Recorder()
    cl, logs, pipe = clients(rec, status="Failed")
    with pytest.raises(BootstrapStop, match="GitHub App installation") as exc:
        bootstrap.run(config(), cl, logs=logs, session_region=REGION, assembly=asm, bootstrap_dir=tmp_path / "boot", approve=lambda p: True, runner=ok_runner(rec), sleep=lambda _s: None, out=lambda _m: None)
    assert exc.value.step == "source-dry-run" and not pipe.enabled
    assert "FilippoLentoni/FinanceLambdasTool" in exc.value.message


@needs_node
def test_account_and_region_mismatch_stop_before_changes(asm, tmp_path):
    rec = Recorder()
    cl, logs, _ = clients(rec)
    cl.sts = SimpleNamespace(get_caller_identity=lambda: {"Account": "1" * 12, "Arn": "x"})
    with pytest.raises(BootstrapStop, match="STS account"):
        bootstrap.run(config(), cl, logs=logs, session_region=REGION, assembly=asm, bootstrap_dir=tmp_path / "b1", approve=lambda p: True, runner=ok_runner(rec), out=lambda _m: None)
    cl2, logs2, _ = clients(rec)
    with pytest.raises(BootstrapStop, match="region"):
        bootstrap.run(config(), cl2, logs=logs2, session_region="us-west-2", assembly=asm, bootstrap_dir=tmp_path / "b2", approve=lambda p: True, runner=ok_runner(rec), out=lambda _m: None)
    assert not [e for e in rec.events if e.startswith(("ssm:put", "cdk:", "logs:"))]


# ===================================================================== configuration
def test_connection_is_reused_from_the_platform():
    ssm = FakeSsm(Recorder(), {bootstrap.PLATFORM_CONNECTION_PARAMETER: CONNECTION})
    cfg = bootstrap.load_bootstrap_config({"account_id": ACCOUNT, "primary_region": REGION, "repo": "financialplanning", "pipeline_name": "x"}, ssm)
    assert cfg.codeconnection_arn == CONNECTION and cfg.repo == "financelambdastool"
    assert cfg.pipeline_name == "finplan-shared-financelambdastool-pipeline" and cfg.github_repository == "FilippoLentoni/FinanceLambdasTool"
    with pytest.raises(BootstrapStop, match="CodeConnection"):
        bootstrap.load_bootstrap_config({"account_id": ACCOUNT, "primary_region": REGION}, FakeSsm(Recorder()))


def test_local_config_reads_the_shared_file_and_overlay(tmp_path):
    shared = tmp_path / "bootstrap.json"
    shared.write_text(json.dumps({"account_id": ACCOUNT, "primary_region": REGION, "repo": "financialplanning", "budget_notification_email": "x"}))
    overlay = tmp_path / "financelambdastool-bootstrap.json"
    overlay.write_text(json.dumps({"direct_test_principal_name": "owner-operator"}))
    data = bootstrap.read_local_config(shared, environ={}, overlay=overlay, repo_root=tmp_path / "repo")
    assert data == {"account_id": ACCOUNT, "primary_region": REGION, "direct_test_principal_name": "owner-operator"}
    repo = tmp_path / "repo"
    repo.mkdir()
    inside = repo / "bootstrap.json"
    inside.write_text("{}")
    with pytest.raises(BootstrapStop, match="inside the repository"):
        bootstrap.read_local_config(inside, environ={}, overlay=None, repo_root=repo)


@pytest.mark.parametrize("value", ["root", ":".join(["arn", "aws", "iam", "", ACCOUNT, "role/x"]), "a*", "a,b"])
def test_direct_test_name_refused_in_local_config(value):
    with pytest.raises(BootstrapStop):
        bootstrap.direct_test_names({"direct_test_principal_name": value})


def test_direct_test_names_per_environment():
    assert bootstrap.direct_test_names({}) == {}
    assert bootstrap.direct_test_names({"direct_test_principal_name": "owner"}) == {"beta": "owner", "gamma": "owner", "prod": "owner"}
    assert bootstrap.direct_test_names({"direct_test_principal_names": {"beta": "owner"}}) == {"beta": "owner"}


def test_tool_limits_are_never_overwritten():
    rec = Recorder()
    ssm = FakeSsm(rec, {"/finplan/beta/financelambdastool/config/tool-limits": '{"user":"edited"}'})
    written = bootstrap.write_tool_limits(ssm, out=lambda _m: None)
    assert "/finplan/beta/financelambdastool/config/tool-limits" not in written and len(written) == 2
    assert ssm.values["/finplan/beta/financelambdastool/config/tool-limits"] == '{"user":"edited"}'


def test_codebuild_log_retention_is_idempotent():
    rec = Recorder()

    class Exists(Exception):
        response = {"Error": {"Code": "ResourceAlreadyExistsException"}}

    logs = FakeLogs(rec)

    def create(logGroupName, tags):
        raise Exists()

    logs.create_log_group = create
    assert bootstrap.apply_codebuild_log_retention(logs, ["p1"], out=lambda _m: None) == ["/aws/codebuild/p1"]
    assert logs.retention == {"/aws/codebuild/p1": 30}


def test_bootstrap_code_passes_the_leak_scan():
    from finplan_contracts import leak_scan

    _, findings = leak_scan.scan_paths([Path(bootstrap.__file__), Path(bootstrap.ROOT) / "docs" / "bootstrap.md"])
    assert findings == []
