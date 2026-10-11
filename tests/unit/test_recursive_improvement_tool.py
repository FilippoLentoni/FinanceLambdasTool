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


@pytest.mark.parametrize("tool", ["run_recursive_improvement", "get_classical_analysis"])
def test_nested_public_research_citations_survive_real_pipeline_and_retrieval(offline, invoke, tool):
    document = recursive_evidence()
    document["evidence"] = [{"source_type": "performance"}, {"source_type": "feedback"}, {
        "source_type": "research_review", "sources": [
            {"url": "https://arxiv.org/abs/2305.01234", "title": "Portfolio policy research"},
            {"url": "https://doi.org/10.1000/example", "title": "Optimization research"}]}]
    client = Client(document)
    wire(offline, client)
    request = {"dry_run": True} if tool == "run_recursive_improvement" else {"analysis_id": document["analysis_id"]}
    result = invoke(offline, tool, request, source="ci_test")
    assert result == document, result
    assert len(client.calls) == 1 and offline.jobs.count() == 0


@pytest.mark.parametrize("tool", ["run_recursive_improvement", "get_classical_analysis"])
@pytest.mark.parametrize("url", [
    "s3:" + "//private-research/artifact.json",
    "https://private-research" + ".s3.amazonaws.com/artifact.json",
    "https://127.0.0.1/research", "https://127.1/research", "https://10.0.0.1/research", "https://169.254.169.254/latest",
    "https://[::1]/research", "https://[fd00::1]/research", "https://research.internal/paper",
    "https://localhost/paper", "https://research.local/paper", "https://research/paper",
    "https://user:password@arxiv.org/paper",
])
def test_nested_storage_and_private_citations_are_not_returned(offline, invoke, tool, url):
    document = recursive_evidence()
    document["evidence"] = [{"source_type": "research_review", "sources": [{"url": url}]}]
    client = Client(document)
    wire(offline, client)
    request = {"dry_run": True} if tool == "run_recursive_improvement" else {"analysis_id": document["analysis_id"]}
    result = invoke(offline, tool, request)
    assert result["code"] == "INTERNAL", result
    assert url not in str(result) and len(client.calls) == 1


@pytest.mark.parametrize("tool", ["run_recursive_improvement", "get_classical_analysis"])
@pytest.mark.parametrize("location", ["source_metadata", "evidence_sibling", "evidence_deeper", "top_level"])
def test_nested_citation_allowance_does_not_exempt_other_storage_locations(offline, invoke, tool, location):
    document = recursive_evidence()
    document["evidence"] = [{"source_type": "research_review", "sources": [{"url": "https://arxiv.org/abs/2305.01234"}]}]
    leaked = "s3:" + "//private-research/artifacts/result.json"
    if location == "source_metadata":
        document["evidence"][0]["sources"][0]["artifact_location"] = leaked
    elif location == "evidence_sibling":
        document["evidence"][0]["artifact_location"] = leaked
    elif location == "evidence_deeper":
        document["evidence"][0]["extra"] = {"sources": [{"url": leaked}]}
    else:
        document["artifact_location"] = leaked
    client = Client(document)
    wire(offline, client)
    request = {"dry_run": True} if tool == "run_recursive_improvement" else {"analysis_id": document["analysis_id"]}
    result = invoke(offline, tool, request)
    assert result["code"] == "INTERNAL", result
    assert leaked not in str(result) and len(client.calls) == 1


def test_analysis_cannot_skip_citation_validation_by_adding_a_list_field(offline, invoke):
    document = recursive_evidence()
    document["analyses"] = []
    document["evidence"] = [{"sources": [{"url": "https://private-research" + ".s3.amazonaws.com/artifact.json"}]}]
    client = Client(document)
    wire(offline, client)
    result = invoke(offline, "get_classical_analysis", {"analysis_id": document["analysis_id"]})
    assert result["code"] == "INTERNAL", result


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


def benchmark_evidence(family="swarm_mode_a", amount=1.):
    import json
    document = recursive_evidence()
    request = json.loads((contracts_root()/"fixtures/tools/submit-experiment-request/valid/research.json").read_text())
    request.update(job_type=family, dry_run=True, purpose="research")
    request["configuration"]["payload"].update(strategy="qwen_swarm" if family == "swarm_mode_a" else "jev", objective="llm_benchmark")
    category = "gpu" if family == "swarm_mode_a" else "cpu_research"
    document["proposed_experiment"] = {"job_type": family, "budget_category": category, "tool_request": {"name": "submit_experiment", "arguments": request}}
    document["cost_estimate"] = {**evidence("research_run")["cost_estimate"], "estimated_usd_upper_bound": amount, "budget_category": category}
    return document


@pytest.mark.parametrize("family,amount", [("swarm_mode_a", 1.), ("jev_backtest", .75)])
def test_typed_benchmark_uses_existing_category_cap_and_same_cycle_paid_path(offline, invoke, family, amount):
    document = benchmark_evidence(family, amount)
    client = Client(document)
    wire(offline, client)
    result = invoke(offline, "run_recursive_improvement", {"cycle_id": AID, "dry_run": False, "confirmed_by_user": True, "idempotency_key": "benchmark-cycle-test"})
    assert "code" not in result, result
    import json
    calls = [json.loads(row["Payload"]) for row in client.calls]
    assert len(calls) == 2 and all(call["operation"] == "run_recursive_improvement" for call in calls)
    assert all(call["request"]["cycle_id"] == AID for call in calls)
    assert calls[0]["request"]["dry_run"] is True and calls[1]["request"]["dry_run"] is False


@pytest.mark.parametrize("defect", ["strategy", "family", "category", "invalid_request", "over_category_cap"])
def test_invalid_benchmark_cannot_escape_research_caps(offline, invoke, defect):
    document = benchmark_evidence()
    proposal = document["proposed_experiment"]
    if defect == "strategy":
        proposal["tool_request"]["arguments"]["configuration"]["payload"]["strategy"] = "ppo"
    elif defect == "family":
        proposal["job_type"] = "jev_backtest"
    elif defect == "category":
        document["cost_estimate"]["budget_category"] = "cpu_research"
    elif defect == "invalid_request":
        proposal["tool_request"]["arguments"]["input_snapshot_id"] = "unknown"
    else:
        document["cost_estimate"]["estimated_usd_upper_bound"] = 5.01
    client = Client(document)
    wire(offline, client)
    result = invoke(offline, "run_recursive_improvement", {"cycle_id": AID, "dry_run": False, "confirmed_by_user": True, "idempotency_key": "benchmark-cycle-test"})
    assert result["code"] == "BUDGET_EXCEEDED", result
    assert len(client.calls) == 1


def test_existing_gpu_approval_job_is_readonly_without_another_paid_call(offline, invoke):
    document = benchmark_evidence()
    document["job"] = {"run_id": "run_01JA2B3C4D5E6F7G8H9JKMNPQR", "state": "awaiting_approval"}
    document.pop("cost_estimate")
    client = Client(document)
    wire(offline, client)
    result = invoke(offline, "run_recursive_improvement", {"cycle_id": AID, "dry_run": False, "confirmed_by_user": True, "idempotency_key": "benchmark-cycle-test"})
    assert result == document and len(client.calls) == 1


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
