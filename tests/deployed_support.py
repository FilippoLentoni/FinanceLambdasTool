"""Helpers of the DEPLOYED suites (``tests/integration`` in beta and gamma, ``tests/smoke`` in prod).

Platform market data may be REAL (phase 2, ``yfinance`` lineage) or SYNTHETIC in any environment
(decision 26: each environment ingests independently; prod may still serve fixture snapshots while it
transitions). Request fixtures and plan records stay synthetic.

Lesson L5: these suites run REAL calls. ``scripts/stage_runner.py tests`` starts them in the
environment's stage project with ``FINPLAN_TARGET_ENV`` set; they use the stage role's credentials
from the default chain (never the offline fake keys) and invoke the deployed tool Lambdas directly
(alias ``current``, resolved from ``/finplan/<env>/financelambdastool/lambda/<tool>-arn``). Each tool
then makes real SigV4 calls to the deployed producers of the same environment. Offline, every
deployed test is skipped; the stage runner fails a stage whose suite executed no test.
"""

from __future__ import annotations

import json
import os
import uuid
from functools import lru_cache
from typing import Any

import pytest

from finplan_tools.core.registry import CATALOG

__all__ = [
    "TARGET_ENV",
    "check_snapshot_provenance",
    "deployed",
    "fixture_request",
    "integration_snapshot_id",
    "invoke",
    "is_error",
    "lambda_client",
    "ssm_client",
    "tool_ref",
    "validate_result",
]

TARGET_ENV = os.environ.get("FINPLAN_TARGET_ENV") or ""
deployed = pytest.mark.skipif(not TARGET_ENV, reason="deployed suite: started by scripts/stage_runner.py with FINPLAN_TARGET_ENV (real AWS calls as the stage role)")
#: Codes that mean broken wiring or permissions, never an acceptable outcome of a deployed call.
WIRING_FAILURES = ("UNAUTHORIZED", "FORBIDDEN", "INTERNAL")


def _region() -> str:
    from finplan_tools.core.aws_clients import region

    return region()


@lru_cache(maxsize=1)
def lambda_client() -> Any:
    import boto3
    from botocore.config import Config

    return boto3.session.Session(region_name=_region()).client("lambda", config=Config(signature_version="v4", read_timeout=90, retries={"max_attempts": 2, "mode": "standard"}))


@lru_cache(maxsize=1)
def ssm_client() -> Any:
    from finplan_tools.core.aws_clients import ssm_client as make

    return make(_region())


@lru_cache(maxsize=1)
def account() -> str:
    import boto3

    return boto3.session.Session(region_name=_region()).client("sts").get_caller_identity()["Account"]


def tool_ref(tool: str, env: str | None = None) -> str:
    """The published, alias-qualified reference of ``tool`` (REL-01)."""
    name = f"/finplan/{env or TARGET_ENV}/financelambdastool/lambda/{CATALOG[tool].lambda_ref_name}"
    return ssm_client().get_parameter(Name=name)["Parameter"]["Value"]


def invoke(tool: str, arguments: Any, *, source: str = "ci_test", function: str | None = None, **invocation: Any) -> dict[str, Any]:
    """Invoke the deployed tool directly; the handler never raises, so a FunctionError is a failure
    (for example a bundle that does not import: lesson L3)."""
    inv = {"source": source, "environment": TARGET_ENV, "correlation_id": f"ci-{uuid.uuid4().hex[:24]}", **invocation}
    event = {"finplan_invocation": inv, "tool": tool, "arguments": arguments}
    resp = lambda_client().invoke(FunctionName=function or tool_ref(tool), InvocationType="RequestResponse", Payload=json.dumps(event).encode("utf-8"))
    payload = resp["Payload"].read().decode("utf-8")
    assert "FunctionError" not in resp, f"{tool} raised in Lambda: {payload[:2000]}"
    doc = json.loads(payload)
    if is_error(doc):
        assert doc["correlation_id"] == inv["correlation_id"], "correlation_id must be propagated"
    return doc


def is_error(doc: Any) -> bool:
    return isinstance(doc, dict) and {"code", "message", "retryable", "correlation_id"} <= set(doc)


def validate_result(tool: str, doc: dict[str, Any]) -> None:
    """The result validates against the pinned tool response schema or the error envelope."""
    from finplan_tools.core.contracts import validate_document

    schema = "error" if is_error(doc) else CATALOG[tool].output_schema
    res = validate_document(doc, schema)
    assert res.valid, f"{tool}: {schema}: {[i.message for i in res.issues][:5]}"


def fixture_request(tool: str) -> dict[str, Any]:
    """The first valid contract fixture of ``tools/<tool>-request`` (synthetic, never real data)."""
    from finplan_contracts.schemas import contracts_root

    folder = contracts_root() / "fixtures" / "tools" / f"{tool.replace('_', '-')}-request" / "valid"
    files = sorted(folder.glob("*.json"))
    assert files, f"no contract fixture for {tool}"
    return json.loads(files[0].read_text(encoding="utf-8"))


#: Lineage providers of the platform's synthetic fixture/mock data (test data only).
SYNTHETIC_PROVIDERS = ("fixture", "mock")


def integration_snapshot_id(env: str | None = None) -> str | None:
    """The approved snapshot the platform's lifecycle suite last used in ``env``
    (``/finplan/<env>/financialplanning/config/integration-snapshot-id``), or None when the platform
    has not published one (for example prod, whose platform suite is a smoke)."""
    from botocore.exceptions import ClientError

    try:
        value = ssm_client().get_parameter(Name=f"/finplan/{env or TARGET_ENV}/financialplanning/config/integration-snapshot-id")["Parameter"]["Value"]
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") == "ParameterNotFound":
            return None
        raise
    return value.strip() or None


def check_snapshot_provenance(doc: dict[str, Any], *, request_synthetic: bool = False) -> str:
    """Accept REAL or SYNTHETIC platform market data in every environment (decision 26, data parity).

    Each environment ingests independently: beta and gamma serve phase 2 data (real ``yfinance``
    snapshots, no ``synthetic`` flag), prod may still serve synthetic fixture snapshots during the
    transition. Either is valid; what must hold is that the tool surfaces the platform's provenance
    unchanged: a real snapshot names a real provider in its lineage and the response is not marked
    synthetic unless the caller flagged the request, and a synthetic snapshot keeps its flag.
    Returns ``"real"`` or ``"synthetic"``.
    """
    snapshot = doc["snapshot"]
    lineage = snapshot.get("lineage") or {}
    provider = lineage.get("provider") if isinstance(lineage, dict) else None
    if snapshot.get("synthetic") is True:
        assert doc.get("synthetic") is True, "a synthetic snapshot must keep synthetic: true in the tool response"
        return "synthetic"
    assert provider and provider not in SYNTHETIC_PROVIDERS, f"a snapshot without synthetic: true must name a real provider in its lineage (got {provider!r})"
    if not request_synthetic:
        assert doc.get("synthetic") is not True, "the tool must not mark real platform data as synthetic"
    return "real"
