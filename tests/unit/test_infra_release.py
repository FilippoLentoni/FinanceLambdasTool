"""Release publication, pre-deploy checks and stage actions (tasks 6.2a, 8.3, 8.6, 9.1-9.4;
REL-01..REL-04, REL-06, REL-07, EXP-15, ENVW-08; lesson L5). Offline: moto SSM and fakes."""

from __future__ import annotations

import io
import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
import boto3
from moto import mock_aws

from finplan_tools.core.registry import CATALOG, ROLE_CLASSES
from infra.stacks import naming as n
from infra.stacks.tools import output_key, role_output_key
from scripts import release
from scripts.predeploy import PredeployError, normalize_direct_test_principal, normalize_gateway_principal, parse_endpoint, resolve
from tests.unit.infra_support import ACCOUNT, REGION

RID = "rel_01KDVDNAZ83BAMMYCEGWF33DPM"
RID2 = "rel_01KDVDNB0000000000000000AA"
DESCRIPTIONS = {t: f"{t.replace('_', ' ')} (synthetic test description)" for t in CATALOG}


def arn(service: str, resource: str) -> str:
    return ":".join(["arn", "aws", service, REGION if service != "iam" else "", ACCOUNT, resource])


def endpoint(api_id: str, stage: str, path: str = "") -> str:
    return "https://" + api_id + ".execute-api." + REGION + ".amazonaws.com/" + stage + path


def outputs(env: str) -> dict[str, str]:
    out = {output_key(t): arn("lambda", f"function:{n.function_name(env, t)}:current") for t in CATALOG}
    out.update({role_output_key(c): arn("iam", f"role/{n.role_class_role_name(env, c)}") for c in ROLE_CLASSES})
    return out


class FakeCfn:
    def __init__(self, env: str, out: dict[str, str] | None = None) -> None:
        self.out = outputs(env) if out is None else out

    def describe_stacks(self, StackName):
        return {"Stacks": [{"StackName": StackName, "Outputs": [{"OutputKey": k, "OutputValue": v} for k, v in self.out.items()]}]}


def info(rid: str = RID, **kw) -> release.ReleaseInfo:
    base = {"release_id": rid, "source_commit": "a" * 40, "artifact_digest": "sha256:" + "1" * 64, "contract_version": "1.0.0", "contract_digest": "sha256:" + "2" * 64, "served_contract_majors": [1], "region": REGION, "built_at": "2026-10-08T00:00:00Z"}
    base.update(kw)
    return release.ReleaseInfo(**base)


@pytest.fixture
def ssm():
    with mock_aws():
        import boto3

        yield boto3.client("ssm", region_name=REGION)


def _value(ssm, name):
    return ssm.get_parameter(Name=name)["Parameter"]["Value"]


# ===================================================================== 9.1 publication
def test_tool_catalog_validates_against_the_pinned_schema():
    """REL-02: name, description, schema $ids (resolve in the pinned package), state-changing flag,
    role class and this environment's Lambda reference parameter."""
    doc = release.build_tool_catalog("gamma", RID, "1.0.0", DESCRIPTIONS)
    assert {t["name"] for t in doc["tools"]} == set(CATALOG)
    for t in doc["tools"]:
        assert t["lambda_ref_parameter"] == f"/finplan/gamma/financelambdastool/lambda/{CATALOG[t['name']].lambda_ref_name}"
        assert t["state_changing"] == CATALOG[t["name"]].state_changing and t["role_class"] == CATALOG[t["name"]].role_class
    from finplan_tools.core.contracts import store

    ids = {info.id for info in store()}
    assert all(t["input_schema_id"] in ids and t["output_schema_id"] in ids for t in doc["tools"])
    with pytest.raises(release.ManifestError, match="without an implementation"):
        release.build_tool_catalog("gamma", RID, "1.0.0", {k: v for k, v in DESCRIPTIONS.items() if k != "get_plan"})


def test_publish_release_writes_references_catalog_and_manifest(ssm):
    """REL-01, REL-02, REL-03, task 8.6."""
    m = release.publish_release(info(), "gamma", ssm=ssm, cfn=FakeCfn("gamma"), descriptions=DESCRIPTIONS, now=datetime(2026, 10, 8, tzinfo=UTC))
    for tool, e in CATALOG.items():
        ref = _value(ssm, f"/finplan/gamma/financelambdastool/lambda/{e.lambda_ref_name}")
        assert ref.endswith(":current") and n.function_name("gamma", tool) in ref
        assert m["outputs"][e.lambda_ref_name] == f"/finplan/gamma/financelambdastool/lambda/{e.lambda_ref_name}"
    for cls in ROLE_CLASSES:
        assert _value(ssm, f"/finplan/gamma/financelambdastool/lambda/role-{cls}-arn").endswith(f"role/{n.role_class_role_name('gamma', cls)}")
    catalog = json.loads(_value(ssm, "/finplan/gamma/financelambdastool/contract/tool-catalog"))
    assert catalog["release_id"] == RID and catalog["environment"] == "gamma"
    assert m["outputs"]["tool-catalog"] == "/finplan/gamma/financelambdastool/contract/tool-catalog"
    stored = json.loads(_value(ssm, "/finplan/gamma/financelambdastool/release/manifest"))
    assert stored == m and stored["previous_release_id"] is None and stored["served_contract_majors"] == [1]
    assert _value(ssm, "/finplan/gamma/financelambdastool/release/current-release-id") == RID


def test_budget_enforced_role_names_is_the_submitter_role_only(ssm):
    """Task 8.6: the parameter lists the submitter role only; no ARN, no account ID."""
    release.publish_release(info(), "beta", ssm=ssm, cfn=FakeCfn("beta"), descriptions=DESCRIPTIONS)
    value = _value(ssm, "/finplan/beta/financelambdastool/config/budget-enforced-role-names")
    assert value == "finplan-beta-financelambdastool-tool-role-submitter"
    assert "arn:" not in value and ACCOUNT not in value
    from finplan_contracts import ssm as contract_ssm

    assert contract_ssm.validate_value("/finplan/beta/financelambdastool/config/budget-enforced-role-names", value) == []


def test_previous_release_rollback_and_prod_approval(ssm):
    release.publish_release(info(), "prod", ssm=ssm, cfn=FakeCfn("prod"), descriptions=DESCRIPTIONS, approval={"approved_by": "owner", "approved_at": "2026-10-08T01:00:00Z"})
    with pytest.raises(release.ManifestError, match="approval"):
        release.publish_release(info(RID2), "prod", ssm=ssm, cfn=FakeCfn("prod"), descriptions=DESCRIPTIONS)
    m2 = release.publish_release(info(RID2), "prod", ssm=ssm, cfn=FakeCfn("prod"), descriptions=DESCRIPTIONS, approval={"approved_by": "owner", "approved_at": "2026-10-08T02:00:00Z"})
    assert m2["previous_release_id"] == RID and m2["approved_by"] == "owner"
    # REL-07: rollback to RID records rolled_back_from RID2
    m3 = release.publish_release(info(RID, rollback=True), "prod", ssm=ssm, cfn=FakeCfn("prod"), descriptions=DESCRIPTIONS, approval={"approved_by": "owner", "approved_at": "2026-10-08T03:00:00Z"})
    assert m3["rolled_back_from"] == RID2 and m3["release_id"] == RID
    assert _value(ssm, "/finplan/prod/financelambdastool/release/current-release-id") == RID


def test_same_digest_in_every_environment(ssm):
    """REL-04: one build output, three manifests, one artifact digest."""
    digests = set()
    for env in ("beta", "gamma", "prod"):
        approval = {"approved_by": "owner", "approved_at": "2026-10-08T01:00:00Z"} if env == "prod" else None
        digests.add(release.publish_release(info(), env, ssm=ssm, cfn=FakeCfn(env), descriptions=DESCRIPTIONS, approval=approval)["artifact_digest"])
    assert len(digests) == 1


def test_unqualified_reference_and_cross_repo_writes_are_refused(ssm):
    out = outputs("beta")
    out[output_key("get_plan")] = out[output_key("get_plan")].removesuffix(":current")
    with pytest.raises(release.ManifestError, match="alias-qualified"):
        release.publish_release(info(), "beta", ssm=ssm, cfn=FakeCfn("beta", out), descriptions=DESCRIPTIONS)
    with pytest.raises(release.ManifestError):
        release._put(ssm, "beta", "/finplan/beta/financialplanning/api/plan-endpoint", "x")
    with pytest.raises(release.ManifestError):
        release._put(ssm, "beta", "/finplan/gamma/financelambdastool/release/current-release-id", RID)


def test_assembly_digest_and_ledger_roundtrip(tmp_path):
    """REL-04 digest; REL-07 rollback re-emits the stored build output without rebuilding."""
    out = tmp_path / "build-output"
    (out / "cdk.out").mkdir(parents=True)
    (out / "cdk.out" / "manifest.json").write_text("{}")
    (out / "cdk.out" / "a.template.json").write_text('{"Resources": {}}')
    digest = release.assembly_digest(out / "cdk.out")
    assert digest == release.assembly_digest(out / "cdk.out") and digest.startswith("sha256:")
    ri = info(artifact_digest=digest)
    (out / "release-info.json").write_text(ri.to_json())

    class FakeS3:
        def __init__(self):
            self.objects = {}

        def put_object(self, Bucket, Key, Body, **kw):
            if kw.get("IfNoneMatch") == "*" and (Bucket, Key) in self.objects:
                raise RuntimeError("PreconditionFailed")
            self.objects[(Bucket, Key)] = Body

        def get_object(self, Bucket, Key):
            return {"Body": io.BytesIO(self.objects[(Bucket, Key)])}

    s3 = FakeS3()
    release.store_build_output(s3, "store", ri, out)
    with pytest.raises(RuntimeError):
        release.store_build_output(s3, "store", ri, out)  # write-once
    again = release.fetch_build_output(s3, "store", RID, tmp_path / "rollback")
    assert again.rollback is True and again.artifact_digest == digest
    # a tampered ledger entry is refused
    key = ("store", f"releases/{RID}/build-output.zip")
    import zipfile

    buf = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(s3.objects[key])) as src, zipfile.ZipFile(buf, "w") as dst:
        for item in src.infolist():
            data = src.read(item)
            dst.writestr(item, data + b" " if item.filename.endswith("a.template.json") else data)
    s3.objects[key] = buf.getvalue()
    with pytest.raises(release.ManifestError, match="digest"):
        release.fetch_build_output(s3, "store", RID, tmp_path / "rollback2")


# ===================================================================== pre-deploy (REL-06, 6.2a, 8.2, 8.3)
class ParameterNotFound(Exception):
    pass


class DictSsm:
    def __init__(self, values: dict[str, str]) -> None:
        self.values = dict(values)
        self.reads: list[str] = []

    def get_parameter(self, Name):
        self.reads.append(Name)
        if Name not in self.values:
            raise ParameterNotFound(Name)
        return {"Parameter": {"Name": Name, "Value": self.values[Name]}}


def manifest(repo: str, env: str, majors=(1,)) -> str:
    return json.dumps({"repo": repo, "environment": env, "served_contract_majors": list(majors)})


def producer_params(env: str, *, model: bool = True) -> dict[str, str]:
    p = {
        f"/finplan/{env}/financialplanning/release/manifest": manifest("financialplanning", env),
        f"/finplan/{env}/financialplanning/api/plan-endpoint": endpoint(f"{env[:4]}plan001"[:10].ljust(10, "0"), "live"),
        f"/finplan/{env}/financialplanning/api/ingestion-endpoint": endpoint(f"{env[:4]}plan001"[:10].ljust(10, "0"), "live", "/v1/ingestions"),
    }
    if model:
        p[f"/finplan/{env}/financemodel/release/manifest"] = manifest("financemodel", env)
        p[f"/finplan/{env}/financemodel/api/job-endpoint"] = endpoint(f"{env[:4]}jobs001"[:10].ljust(10, "0"), "api")
    return p


def test_predeploy_resolves_same_environment_only():
    ssm = DictSsm(producer_params("gamma"))
    res = resolve("gamma", ssm, account=ACCOUNT)
    v = res.variables
    assert v["PLAN_API_ID"] == "gammplan00" and v["PLAN_API_STAGE"] == "live" and v["INGESTION_API_STAGE"] == "live"
    assert v["JOB_API_ID"] == "gammjobs00" and v["JOB_API_STAGE"] == "api"
    assert v["DIRECT_TEST_PRINCIPAL"] == "none" and v["GATEWAY_PRINCIPAL_ROLE_NAME"] == "none"
    assert all(r.startswith(("/finplan/gamma/", "/finplan/shared/")) for r in ssm.reads)  # ENVW-01
    assert any("no direct-test principal" in note for note in res.notes)


def test_platform_absent_blocks_model_absent_proceeds():
    """REL-06."""
    with pytest.raises(PredeployError, match="dependency missing"):
        resolve("gamma", DictSsm({}), account=ACCOUNT)
    res = resolve("beta", DictSsm(producer_params("beta", model=False)), account=ACCOUNT)
    assert res.variables["JOB_API_ID"] == "none"
    assert any("FinanceModel has no release" in note for note in res.notes)
    incompatible = producer_params("gamma")
    incompatible["/finplan/gamma/financialplanning/release/manifest"] = manifest("financialplanning", "gamma", (2,))
    with pytest.raises(PredeployError, match="serves contract majors"):
        resolve("gamma", DictSsm(incompatible), account=ACCOUNT)


def test_per_call_limit_bound_check():
    """EXP-15 / 6.2a: defaults pass; gpu allocation 10 with limit 5.00 fails naming the bound 2.00."""
    params = producer_params("beta")
    resolve("beta", DictSsm(params), account=ACCOUNT)
    params["/finplan/shared/financialplanning/config/budget-allocation"] = json.dumps({"platform_infra": 8, "cpu_research": 7, "bedrock_explanations": 5, "gpu": 10, "reserve": 20})
    with pytest.raises(PredeployError, match=r"2\.00"):
        resolve("beta", DictSsm(params), account=ACCOUNT)
    params["/finplan/shared/financialplanning/config/budget-allocation"] = json.dumps({"platform_infra": 8, "cpu_research": 7, "bedrock_explanations": 5, "gpu": 25, "reserve": 5})
    params["/finplan/beta/financelambdastool/config/tool-limits"] = json.dumps({"max_estimated_usd_per_call": {"cpu_research": 1.0, "unknown_cat": 0}})
    with pytest.raises(PredeployError):
        resolve("beta", DictSsm(params), account=ACCOUNT)


@pytest.mark.parametrize(
    "value",
    [
        ":".join(["arn", "aws", "iam", "", "0" * 12, "role/owner"]),
        "owner*",
        "role/" + "1" * 12,
        "root",
        "owner,other",
        "owner other",
        "group/owner",
        "",
    ],
)
def test_direct_test_name_validation_refuses(value):
    """ENVW-08: ARN, wildcard, account ID, root, list -> refused, and the stage stops before deploy."""
    with pytest.raises(ValueError):
        normalize_direct_test_principal(value)
    params = producer_params("beta")
    params["/finplan/beta/financelambdastool/config/direct-test-principal-name"] = value
    with pytest.raises(PredeployError, match="no invoke grant was changed"):
        resolve("beta", DictSsm(params), account=ACCOUNT)


def test_direct_test_name_accepted_forms():
    assert normalize_direct_test_principal(None) == "none"
    assert normalize_direct_test_principal("owner-operator") == "role/owner-operator"
    assert normalize_direct_test_principal("user/owner") == "user/owner"
    params = producer_params("beta")
    params["/finplan/beta/financelambdastool/config/direct-test-principal-name"] = "owner-operator"
    assert resolve("beta", DictSsm(params), account=ACCOUNT).variables["DIRECT_TEST_PRINCIPAL"] == "role/owner-operator"


def test_gateway_principal_validation():
    """ENVW-03: no grant when absent; another account or environment refused."""
    role = arn("iam", "role/finplan-gamma-financeagent-gateway-role")
    assert normalize_gateway_principal(None, "gamma", ACCOUNT) == "none"
    assert normalize_gateway_principal(role, "gamma", ACCOUNT) == "finplan-gamma-financeagent-gateway-role"
    assert normalize_gateway_principal("finplan-gamma-financeagent-gateway-role", "gamma", ACCOUNT) == "finplan-gamma-financeagent-gateway-role"
    with pytest.raises(ValueError, match="another account"):
        normalize_gateway_principal(role, "gamma", "1" * 12)
    with pytest.raises(ValueError, match="this environment"):
        normalize_gateway_principal(arn("iam", "role/finplan-prod-financeagent-gateway-role"), "gamma", ACCOUNT)


def test_endpoint_parsing():
    assert parse_endpoint(endpoint("abcdefghij", "live", "/v1/ingestions")) == ("abcdefghij", "live")
    for bad in (endpoint("abcdefghij", "live").replace("https:", "http:"), endpoint("abcdefghij", "live").replace(REGION, "us-west-2"), "https://example.invalid/live", None):
        with pytest.raises(ValueError):
            parse_endpoint(bad)


def test_variables_file(tmp_path):
    from scripts.predeploy import write_variables

    write_variables(tmp_path / "v.env", {"DIRECT_TEST_PRINCIPAL": "role/a b", "PLAN_API_ID": "abcdefghij"})
    assert (tmp_path / "v.env").read_text() == "DIRECT_TEST_PRINCIPAL='role/a b'\nPLAN_API_ID=abcdefghij\n"


def test_predeploy_action_checks_the_contract_pin(monkeypatch):
    from scripts import stage_runner

    monkeypatch.setattr("scripts.check_contracts_pin.check", lambda root, env=None, check_installed=True: ["0.x pin is beta-only"])
    with pytest.raises(PredeployError, match="beta-only"):
        stage_runner.predeploy_action("gamma", ssm=DictSsm(producer_params("gamma")), account=ACCOUNT, out=lambda _m: None)


# ===================================================================== lesson L5: stage tests
def _junit(path: Path, tests: int, skipped: int = 0, failures: int = 0) -> None:
    path.write_text(f'<testsuites><testsuite name="s" tests="{tests}" skipped="{skipped}" failures="{failures}" errors="0"></testsuite></testsuites>')


@pytest.mark.parametrize(("tests", "skipped", "rc", "expected"), [(0, 0, 5, 1), (3, 3, 0, 1), (4, 1, 0, 0), (4, 0, 1, 1)])
def test_stage_suite_fails_on_zero_executed_tests(tests, skipped, rc, expected):
    from scripts.stage_runner import tests_action

    seen = {}

    def run(cmd, cwd, env):
        junit = Path(next(a for a in cmd if a.startswith("--junitxml=")).split("=", 1)[1])
        _junit(junit, tests, skipped)
        seen.update(env)
        return SimpleNamespace(returncode=rc)

    assert tests_action("gamma", run=run, environ={"PATH": "/usr/bin"}, release_id=RID, out=lambda _m: None) == expected
    assert seen["FINPLAN_TARGET_ENV"] == "gamma" and seen["FINPLAN_SUITE"] == "gamma" and "AWS_ACCESS_KEY_ID" not in seen


def test_catalog_goes_to_the_store_and_ssm_holds_a_small_pointer(ssm):
    """Regression (first beta deploy): the full catalog exceeded the 8 KB SSM limit."""
    import hashlib

    from moto import mock_aws

    with mock_aws():
        s3 = boto3.client("s3", region_name=REGION)
        s3.create_bucket(Bucket="example-bucket", CreateBucketConfiguration={"LocationConstraint": REGION})
        release.publish_release(info(), "gamma", ssm=ssm, cfn=FakeCfn("gamma"), s3=s3, store_bucket="example-bucket", descriptions=DESCRIPTIONS)
        raw = _value(ssm, "/finplan/gamma/financelambdastool/contract/tool-catalog")
        assert len(raw) < 4096
        pointer = json.loads(raw)
        assert pointer["kind"] == "tool-catalog-pointer" and pointer["release_id"] == RID
        body = s3.get_object(Bucket="example-bucket", Key=release.catalog_object_key(RID, "gamma"))["Body"].read()
        assert hashlib.sha256(body).hexdigest() == pointer["sha256"]
        catalog = json.loads(body)
        assert sorted(t["name"] for t in catalog["tools"]) == pointer["tools"] == sorted(CATALOG)
