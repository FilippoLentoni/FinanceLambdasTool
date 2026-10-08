"""Deployed beta/gamma suite (tasks 10.1-10.3 groundwork; REL-01..REL-03, ENVW-03, ENVW-04, ENVW-06,
ENV-03; lesson L5). REAL calls as the stage role: direct Lambda invocation of every deployed tool,
which makes real SigV4 calls to the same environment's deployed platform and FinanceModel APIs.
Requests are the contract package's synthetic fixtures; nothing touches a non-synthetic portfolio and
no paid job is submitted (FinanceModel phase 1 serves CPU fixture stubs). Platform market data is NOT
assumed synthetic: beta and gamma run the platform's phase 2 (real ``yfinance`` snapshots), and the
suite accepts real or synthetic snapshots alike (decision 26, data parity across stages).

Skipped offline; ``scripts/stage_runner.py tests`` runs it with ``FINPLAN_TARGET_ENV=beta|gamma`` and
fails the stage if nothing executed.
"""

from __future__ import annotations

import json

import pytest

from finplan_tools.core.registry import CATALOG
from infra.stacks import naming as n
from tests.deployed_support import (
    TARGET_ENV,
    WIRING_FAILURES,
    check_snapshot_provenance,
    deployed,
    fixture_request,
    integration_snapshot_id,
    invoke,
    is_error,
    lambda_client,
    ssm_client,
    tool_ref,
    validate_result,
)

pytestmark = [pytest.mark.deployed, deployed, pytest.mark.skipif(TARGET_ENV == "prod", reason="prod runs tests/smoke only")]


@pytest.fixture(scope="module")
def capabilities():
    doc = invoke("describe_capabilities", {})
    validate_result("describe_capabilities", doc)
    assert not is_error(doc), doc
    return {t["name"]: t for t in doc["tools"]}


def test_references_catalog_and_manifest_are_published():
    """REL-01..REL-03: every tool reference is alias-qualified; the catalog and manifest validate."""
    from finplan_tools.core.contracts import validate_document

    ssm = ssm_client()
    for tool in CATALOG:
        assert tool_ref(tool).endswith(f":{n.TOOL_ALIAS}")
    pointer = json.loads(ssm.get_parameter(Name=f"/finplan/{TARGET_ENV}/financelambdastool/contract/tool-catalog")["Parameter"]["Value"])
    assert pointer["kind"] == "tool-catalog-pointer" and pointer["environment"] == TARGET_ENV
    import hashlib

    from finplan_tools.core.aws_clients import s3_client

    bucket, _, key = pointer["s3_uri"].removeprefix("s3://").partition("/")
    body = s3_client().get_object(Bucket=bucket, Key=key)["Body"].read()
    assert hashlib.sha256(body).hexdigest() == pointer["sha256"]
    catalog = json.loads(body)
    assert validate_document(catalog, "tool-catalog").valid and catalog["environment"] == TARGET_ENV
    assert sorted(t["name"] for t in catalog["tools"]) == pointer["tools"]
    manifest = json.loads(ssm.get_parameter(Name=f"/finplan/{TARGET_ENV}/financelambdastool/release/manifest")["Parameter"]["Value"])
    assert validate_document(manifest, "release-manifest").valid
    assert manifest["release_id"] == catalog["release_id"]
    import os

    if os.environ.get("FINPLAN_RELEASE_ID"):
        assert manifest["release_id"] == os.environ["FINPLAN_RELEASE_ID"]


def test_functions_are_arm64_aliases_with_explicit_grants():
    """L3 (arm64 bundle), ENVW-03 (no wildcard principal, grants only on the alias)."""
    client = lambda_client()
    for tool in CATALOG:
        ref = tool_ref(tool)
        cfg = client.get_function_configuration(FunctionName=ref)
        assert cfg["Architectures"] == ["arm64"] and cfg["Runtime"] == "python3.12"
        assert cfg["Environment"]["Variables"]["FINPLAN_ENV"] == TARGET_ENV
        policy = json.loads(client.get_policy(FunctionName=ref)["Policy"])
        for st in policy["Statement"]:
            principal = st["Principal"]
            assert principal != "*" and "*" not in json.dumps(principal)
            assert st["Action"] == "lambda:InvokeFunction"


@pytest.mark.parametrize("tool", sorted(CATALOG))
def test_every_tool_loads_and_validates_input(tool):
    """Every function imports its bundle and answers a contract envelope (never a Lambda error)."""
    doc = invoke(tool, {"unexpected_field": True})
    validate_result(tool, doc)
    assert is_error(doc) and doc["code"] == "VALIDATION_FAILED", doc


def test_describe_capabilities_reports_this_environment(capabilities):
    assert set(capabilities) == set(CATALOG)
    for name in ("get_plan_version", "get_plan", "list_plan_versions", "query_market_data", "refresh_market_data"):
        assert capabilities[name]["available"], capabilities[name]


@pytest.mark.parametrize("tool", ["get_plan", "get_plan_version", "list_plan_versions", "query_market_data"])
def test_platform_reads_reach_the_deployed_platform(tool):
    """Real SigV4 reads with fixture identifiers: the platform answers (a record or NOT_FOUND), never a
    permission or wiring failure."""
    doc = invoke(tool, fixture_request(tool))
    validate_result(tool, doc)
    if is_error(doc):
        assert doc["code"] == "NOT_FOUND", doc


def test_query_market_data_reads_the_platform_integration_snapshot():
    """Decision 26 (data parity): the approved snapshot the platform's lifecycle suite published for
    this environment is read through the tool whether it is REAL (phase 2, ``yfinance`` lineage, no
    ``synthetic`` flag) or SYNTHETIC, with its provenance surfaced unchanged. The request is not
    flagged synthetic so the response reflects the platform's data."""
    sid = integration_snapshot_id()
    if sid is None:
        pytest.skip("the platform has not published an integration snapshot in this environment")
    doc = invoke("query_market_data", {"input_snapshot_id": sid})
    validate_result("query_market_data", doc)
    if is_error(doc):
        # the snapshot may have expired or been superseded since the platform run: never a wiring failure
        assert doc["code"] in ("NOT_FOUND", "PRECONDITION_FAILED", "DEPENDENCY_UNAVAILABLE", "RATE_LIMITED"), doc
        pytest.skip(f"integration snapshot not readable now: {doc['code']}")
    assert doc["snapshot"]["input_snapshot_id"] == sid
    check_snapshot_provenance(doc)


@pytest.mark.parametrize("tool", ["create_override_version", "validate_plan_version", "publish_plan_version"])
def test_plan_writer_calls_are_authorized(tool):
    """The plan-writer role class reaches the plan API (fixture IDs are unknown -> NOT_FOUND or a
    contract precondition), never FORBIDDEN/UNAUTHORIZED."""
    doc = invoke(tool, fixture_request(tool))
    validate_result(tool, doc)
    if is_error(doc):
        assert doc["code"] not in WIRING_FAILURES, doc


def test_refresh_market_data_is_idempotent_against_the_platform():
    """MKT-01/MKT-02 groundwork: ingestion through the platform's provider adapter in this environment
    (fixture provider or, in phase 2, ``yfinance``; the tool never calls a provider); a duplicate
    request returns the same snapshot (one ingestion)."""
    req = fixture_request("refresh_market_data")
    req["idempotency_key"] = f"ci-refresh-{TARGET_ENV}-0001"
    first = invoke("refresh_market_data", req)
    validate_result("refresh_market_data", first)
    if is_error(first):
        assert first["code"] in ("RATE_LIMITED", "BUDGET_EXCEEDED", "DEPENDENCY_UNAVAILABLE") and first["code"] not in WIRING_FAILURES, first
        pytest.skip(f"platform ingestion answered {first['code']} (retryable outcome passed through)")
    second = invoke("refresh_market_data", req)
    assert second.get("input_snapshot_id") == first.get("input_snapshot_id")


@pytest.mark.parametrize("tool", ["get_job_status", "get_experiment_result", "submit_experiment"])
def test_experiment_tools_follow_model_availability(tool, capabilities):
    """EXP-02: DEPENDENCY_UNAVAILABLE before the FinanceModel release; afterwards real calls to the job
    API (fixture IDs are unknown, so NOT_FOUND or a contract precondition)."""
    doc = invoke(tool, fixture_request(tool))
    validate_result(tool, doc)
    if not capabilities[tool]["available"]:
        assert is_error(doc) and doc["code"] in ("DEPENDENCY_UNAVAILABLE", "UNSUPPORTED_CONTRACT_VERSION"), doc
    elif is_error(doc):
        assert doc["code"] not in WIRING_FAILURES, doc


def test_schema_version_mismatch_and_environment_mismatch():
    """TRH-02 and TRH-05 on the deployed function."""
    doc = invoke("get_plan_version", fixture_request("get_plan_version"), contract_version="2.0.0")
    assert is_error(doc) and doc["code"] == "UNSUPPORTED_CONTRACT_VERSION"
    other = "gamma" if TARGET_ENV == "beta" else "beta"
    doc = invoke("get_plan_version", fixture_request("get_plan_version"), environment=other)
    assert is_error(doc) and doc["code"] == "FORBIDDEN"


@pytest.mark.skipif(TARGET_ENV != "gamma", reason="isolation checks run in gamma")
def test_gamma_cannot_reach_prod():
    """ENV-03 / ENVW-02 in the account: the gamma stage role cannot invoke a prod tool or read prod
    configuration (single account, permission boundary and explicit denies)."""
    from botocore.exceptions import ClientError

    from tests.deployed_support import account

    prod_fn = ":".join(["arn", "aws", "lambda", lambda_client().meta.region_name, account(), "function", n.function_name("prod", "get_plan"), n.TOOL_ALIAS])
    with pytest.raises(ClientError) as exc:
        lambda_client().invoke(FunctionName=prod_fn, Payload=b"{}")
    assert exc.value.response["Error"]["Code"] in ("AccessDeniedException", "AccessDenied")
    with pytest.raises(ClientError) as exc:
        ssm_client().get_parameter(Name="/finplan/prod/financelambdastool/release/manifest")
    assert exc.value.response["Error"]["Code"] in ("AccessDeniedException", "AccessDenied")
