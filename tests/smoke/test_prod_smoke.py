"""Prod smoke suite (task 9.5; REL-08, ENVW-04, CAP-01; lesson L5). REAL, read-only calls as the prod
smoke (stage) role: ``describe_capabilities`` and the read tools with synthetic fixture requests. Prod
platform market data may be REAL (phase 2) or still SYNTHETIC fixture snapshots during the
transition; both are accepted (decision 26). It also confirms that the smoke role cannot invoke any state-changing prod tool (its grant covers
read-only tools only). Skipped offline; run by ``scripts/stage_runner.py tests --env prod``."""

from __future__ import annotations

import pytest

from finplan_tools.core.registry import CATALOG
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
    tool_ref,
    validate_result,
)
from tests.smoke.calls import SMOKE_TOOLS

pytestmark = [pytest.mark.deployed, deployed, pytest.mark.skipif(TARGET_ENV != "prod", reason="the smoke suite runs in prod")]


def test_describe_capabilities():
    doc = invoke("describe_capabilities", {})
    validate_result("describe_capabilities", doc)
    assert not is_error(doc) and doc["environment"] == "prod"
    assert {t["name"] for t in doc["tools"]} == set(CATALOG)


@pytest.mark.parametrize("tool", [t for t in SMOKE_TOOLS if t != "describe_capabilities"])
def test_read_tools(tool):
    doc = invoke(tool, fixture_request(tool))
    validate_result(tool, doc)
    if is_error(doc):
        assert doc["code"] not in WIRING_FAILURES, doc


def test_query_market_data_accepts_real_or_synthetic_prod_snapshots():
    """Decision 26: prod follows gamma to phase 2 in a later release, so a prod snapshot may be REAL
    (``yfinance`` lineage, no ``synthetic`` flag) or still SYNTHETIC; both read cleanly (read-only)."""
    sid = integration_snapshot_id()
    if sid is None:
        pytest.skip("no platform integration snapshot is published in prod")
    doc = invoke("query_market_data", {"input_snapshot_id": sid})
    validate_result("query_market_data", doc)
    if is_error(doc):
        assert doc["code"] not in WIRING_FAILURES, doc
        pytest.skip(f"integration snapshot not readable now: {doc['code']}")
    check_snapshot_provenance(doc)


@pytest.mark.parametrize("tool", sorted(t for t, e in CATALOG.items() if e.state_changing))
def test_smoke_role_cannot_invoke_prod_write_tools(tool):
    """ENVW-04: the prod grants of the smoke role cover read-only tools only (the call is denied
    before the function runs, so nothing is written)."""
    from botocore.exceptions import ClientError

    with pytest.raises(ClientError) as exc:
        lambda_client().invoke(FunctionName=tool_ref(tool), Payload=b"{}")
    assert exc.value.response["Error"]["Code"] in ("AccessDeniedException", "AccessDenied")
