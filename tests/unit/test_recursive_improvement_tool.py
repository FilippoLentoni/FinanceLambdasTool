"""The real MCP pipeline enforces recursive-research identity, budgets and evidence fidelity."""
from copy import deepcopy
from types import SimpleNamespace

import pytest

from finplan_contracts.schemas import contracts_root
from finplan_tools.core.registry import CATALOG
from finplan_tools.handler import invoke as invoke_gateway
from tests.unit.test_classical_tools import AID, Client, evidence, wire


def recursive_evidence():
    import json
    return json.loads((contracts_root()/"fixtures/tools/run-recursive-improvement-response/valid/review.json").read_text())


def gateway_context():
    return SimpleNamespace(client_context=SimpleNamespace(custom={"bedrockAgentCoreToolName": "classical___run_recursive_improvement"}))


def test_default_readonly_recursive_call_preserves_immutable_lineage(offline, invoke):
    document = recursive_evidence()
    client = Client(document)
    wire(offline, client)
    result = invoke(offline, "run_recursive_improvement", {"query": "Review PPO horizon evidence"}, source="ci_test")
    assert result == document
    assert len(client.calls) == 1
    import json
    payload = json.loads(client.calls[0]["Payload"])
    assert payload["operation"] == "run_recursive_improvement"
    assert payload["request"] == {"query": "Review PPO horizon evidence"}
    assert offline.jobs.count() == 0


@pytest.mark.parametrize("amount", [.51, float("inf"), True, -1])
def test_recursive_paid_estimate_cannot_escape_cpu_hard_cap(offline, invoke, amount):
    document = recursive_evidence()
    document["cost_estimate"] = {**evidence("research_run")["cost_estimate"], "estimated_usd_upper_bound": amount}
    # JSON transport cannot encode infinity; direct context still verifies numerical preflight.
    if amount == float("inf"):
        from finplan_tools.tools.classical import _invoke
        from finplan_tools.core.errors import ToolError
        ctx = SimpleNamespace(spec=SimpleNamespace(name="run_recursive_improvement"), limits=offline.runtime.limits(), meta=None,
                              downstream_body=lambda value:value, jobs=SimpleNamespace(classical=lambda *args:document))
        with pytest.raises(ToolError):
            _invoke(ctx, {"cycle_id": AID, "dry_run": False, "confirmed_by_user": True, "idempotency_key": "recursive-test"})
        return
    client = Client(document)
    wire(offline, client)
    result = invoke(offline, "run_recursive_improvement", {"cycle_id": AID, "dry_run": False, "confirmed_by_user": True, "idempotency_key": "recursive-test"})
    assert result["code"] in {"BUDGET_EXCEEDED", "DEPENDENCY_UNAVAILABLE", "INTERNAL"}, result
    assert len(client.calls) == 1


def test_recursive_paid_preflight_derives_same_key_and_launches_once(offline, invoke):
    document = recursive_evidence()
    document["cost_estimate"] = {**evidence("research_run")["cost_estimate"], "estimated_usd_upper_bound": .04}
    client = Client(document)
    wire(offline, client)
    result = invoke(offline, "run_recursive_improvement", {"cycle_id": AID, "dry_run": False, "confirmed_by_user": True, "idempotency_key": "recursive-test"})
    assert "code" not in result, result
    import json
    payloads = [json.loads(row["Payload"])["request"] for row in client.calls]
    assert len(payloads) == 2 and payloads[0]["dry_run"] is True and payloads[1]["dry_run"] is False
    assert payloads[0]["idempotency_key"] == payloads[1]["idempotency_key"]
    assert payloads[1]["idempotency_key"].startswith("lt_")


@pytest.mark.parametrize("state", ["experiment_running", "proposal_ready", "stopped"])
def test_resuming_non_launchable_cycle_returns_evidence_without_cost_or_second_call(offline, invoke, state):
    document = {**recursive_evidence(), "state": state}
    document.pop("cost_estimate", None)
    client = Client(document)
    wire(offline, client)
    result = invoke(offline, "run_recursive_improvement", {"cycle_id": AID, "dry_run": False, "confirmed_by_user": True, "idempotency_key": "recursive-test"})
    assert result == document and len(client.calls) == 1


def test_gateway_paid_recursive_call_requires_verified_transport_user_before_producer(offline, invoke):
    client = Client(recursive_evidence())
    wire(offline, client)
    result = invoke_gateway("run_recursive_improvement", {"cycle_id": AID, "dry_run": False, "confirmed_by_user": True, "idempotency_key": "recursive-test"}, gateway_context(), offline.runtime)
    assert result["code"] == "FORBIDDEN" and not client.calls


def test_gateway_verified_viewer_cannot_start_recursive_compute(offline, invoke):
    client = Client(recursive_evidence())
    wire(offline, client)
    offline.runtime.gateway_research_authorizer = SimpleNamespace(verify_context=lambda _:SimpleNamespace(identity="cognito:verified-user", groups=frozenset({"viewer"})))
    result = invoke_gateway("run_recursive_improvement", {"cycle_id": AID, "dry_run": False, "confirmed_by_user": True, "idempotency_key": "recursive-test"}, gateway_context(), offline.runtime)
    assert result["code"] == "FORBIDDEN" and not client.calls


def test_recursive_adapter_requires_new_producer_release(offline, invoke):
    client = Client(recursive_evidence())
    wire(offline, client)
    offline.runtime.manifest = lambda _: {"contract_version": "1.5.0", "served_contract_majors": [1]}
    result = invoke(offline, "run_recursive_improvement", {"dry_run": True})
    assert result["code"] == "DEPENDENCY_UNAVAILABLE" and not client.calls
    assert CATALOG["run_recursive_improvement"].min_producer_contract == "1.6.0"


def test_horizon_evidence_passes_unchanged_through_existing_adapter(offline, invoke):
    document = evidence("performance")
    document["horizon_evaluation"] = {"version": "finplan-decision-horizon/1", "status": "available", "full_policy_replay": True,
        "protocol_status": "predeclared_at_issuance", "available_forward_sessions": 2,
        "protocol": {"primary_horizon_sessions": 64}, "source": {}, "limitations": [],
        "primary_horizon_mature": False, "windows": [{"horizon_sessions": 64, "observed_sessions": 2, "status": "partial"}],
        "evidence_assessment": {"status": "preliminary", "model_error_proven": False, "automatic_activation": False}}
    expected = deepcopy(document)
    client = Client(document)
    wire(offline, client)
    result = invoke(offline, "evaluate_portfolio_decision", {"portfolio_id": "pf_01JA2B3C4D5E6F7G8H9JKMNPQR", "decision_id": "pd_01JA2B3C4D5E6F7G8H9JKMNPQR"})
    assert result == expected
