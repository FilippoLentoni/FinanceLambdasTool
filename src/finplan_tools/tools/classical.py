"""Traditional optimization, immutable explanation evidence and bounded research adapters."""
from __future__ import annotations

from urllib.parse import urlparse

from ..core.budget import check_tool_budget
from ..core.errors import BUDGET_EXCEEDED, FORBIDDEN, PRECONDITION_FAILED, ToolError
from ..core.registry import register_tool
from ._common import producer_doc


def _paid(request):
    return request.get("dry_run") is False


def _authorize_research(invocation, request, tool_fields):
    if not _paid(request):
        return
    if invocation.source != "direct_test" and "researcher" not in invocation.groups:
        raise ToolError(FORBIDDEN, "starting paid research requires an authorized experiment submitter", reason="group_required")
    if request.get("confirmed_by_user") is not True:
        raise ToolError(PRECONDITION_FAILED, "starting paid research requires explicit user confirmation", reason="confirmation_required")


def _validate_reference(doc):
    if "analyses" in doc:
        return
    ref = doc.get("analysis_ref") or {}
    if ref.get("owner") != "financemodel" or ref.get("artifact_id") != doc.get("analysis_id") or ref.get("kind") != "classical_analysis":
        raise ToolError.internal("the producer returned an inconsistent immutable analysis reference")
    for source in doc.get("sources", []):
        parsed = urlparse(source.get("url", ""))
        hostname = (parsed.hostname or "").lower()
        if parsed.scheme not in ("http", "https") or not hostname or parsed.username or parsed.password or hostname.endswith("amazonaws.com") or hostname in {"localhost", "127.0.0.1", "::1"}:
            raise ToolError.internal("the producer returned an invalid public evidence citation")


def _invoke(ctx, request):
    operation = ctx.spec.name
    down = dict(request)
    if "idempotency_key" in down:
        down = ctx.downstream_body(down)
    if operation == "run_portfolio_research" and _paid(request):
        estimate = producer_doc(ctx.jobs.classical(operation, {**down, "dry_run": True}, ctx.meta), "research estimate")
        cost = producer_doc(estimate.get("cost_estimate"), "research cost estimate")
        check_tool_budget(cost, ctx.limits)
        amount = cost.get("estimated_usd_upper_bound")
        if not isinstance(amount, (int, float)) or isinstance(amount, bool) or not 0 <= amount <= .5:
            raise ToolError(BUDGET_EXCEEDED, "weekly research estimate exceeds the USD 0.50 hard cap", limit_usd=.5)
    doc = producer_doc(ctx.jobs.classical(operation, down, ctx.meta), "classical analysis")
    _validate_reference(doc)
    if operation == "get_classical_analysis" and doc.get("analysis_id") != request["analysis_id"]:
        raise ToolError.internal("the producer returned another analysis than requested")
    return doc


_DESCRIPTIONS = {
    "explain_portfolio_decision": "Explain an immutable PPO or traditional decision_id using its frozen issued inputs. Traditional decisions use objective counterfactuals/Shapley; PPO reports policy diagnostics and truthful attribution limits. Does not rerun or accept a new recommendation.",
    "compare_portfolio_decisions": "Compare previous_decision_id and current_decision_id from durable recommendation history, including input snapshots, holdings revisions, targets and strategy identity. Traditional comparable decisions support grouped Shapley; PPO or cross-family comparisons expose descriptive changes and limitations.",
    "evaluate_portfolio_decision": "Evaluate a stored decision against later approved observed prices, saved paper revisions and recorded simulated fills. Distinguish simulated allocation, accepted paper actuals and unavailable broker actuals/calibrated forecasts. Save discrepancy evidence for recurring review.",
    "recommend_classical_portfolio": "Generate and save a traditional portfolio optimization recommendation using min_variance (default), mean_variance or cvar. Uses the saved paper holdings and latest approved completed market snapshot unless explicit identifiers/date are supplied. Returns proposed share changes, input provenance and immutable analysis_id; no trades or holdings updates.",
    "explain_classical_recommendation": "Explain a stored classical recommendation by analysis_id, optionally for one instrument. Uses the frozen optimization inputs, objective versus a keep-holding counterfactual and grouped Shapley attribution. Returns immutable reproducible evidence; attribution describes this model, not market causality.",
    "compare_classical_plans": "Compare two immutable traditional recommendation analyses by previous_analysis_id/current_analysis_id. Attribute changes in allocations to grouped market, holdings and model-setting inputs with Shapley, retaining both snapshots and model provenance.",
    "evaluate_classical_performance": "Evaluate a stored recommendation against an approved observed snapshot/end date. Distinguishes saved paper holdings, simulated recommended allocation and broker actuals; reports expected-versus-observed gaps only where an expectation exists. Saves metric/decomposition evidence and explicitly reports missing actuals or forecast calibration.",
    "get_classical_analysis": "Retrieve a previously issued immutable traditional portfolio recommendation, explanation, comparison, performance review, market event research or feedback record by analysis_id, including its trusted artifact reference/checksum.",
    "list_classical_analyses": "List stored immutable classical portfolio analyses, optionally filtered by portfolio_id and kind. Use these issued identifiers to compare prior dates or retrieve stakeholder evidence. Read-only; no execution or model promotion.",
    "research_portfolio_models": "Create and store a bounded research review using model literature and stored portfolio feedback/performance evidence. Returns dated sources and proposed experiments for the sandbox. Does not launch compute or replace serving strategies.",
    "research_market_events": "Research dated public market/news metadata for a stored portfolio analysis, optionally within a bounded date range/query. Return source URLs, headlines, publication dates and provenance as context, with unavailable sources and causal uncertainty explicit. Does not claim headlines caused a stock move.",
    "submit_portfolio_feedback": "Store bounded user feedback against an immutable portfolio analysis for later weekly research and stakeholder audit. Requires analysis_id, text and idempotency_key. Does not change holdings, investment plans or deployed models.",
    "run_portfolio_research": "Estimate or launch a bounded sandbox experiment proposed by a stored research review. dry_run defaults true. Paid launch requires an authorized experiment submitter, confirmed_by_user=true and idempotency_key; USD 0.50 maximum and one job per week enforced by producer. Never promotes a strategy or executes trades.",
}

for _name, _description in _DESCRIPTIONS.items():
    register_tool(
        _name,
        description=_description,
        list_key="analyses" if _name == "list_classical_analyses" else None,
        grant_pointers=tuple(f"/sources/{index}/url" for index in range(50)),
        writes=_paid if _name == "run_portfolio_research" else None,
        authorize=_authorize_research if _name == "run_portfolio_research" else None,
    )(_invoke)
