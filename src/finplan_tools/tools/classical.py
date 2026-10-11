"""Traditional optimization, immutable explanation evidence and bounded research adapters."""
from __future__ import annotations

from ipaddress import ip_address
from math import isfinite
from urllib.parse import urlparse

from ..core.budget import check_tool_budget
from ..core.errors import BUDGET_EXCEEDED, FORBIDDEN, PRECONDITION_FAILED, ToolError
from ..core.registry import register_tool
from ._common import producer_doc


def _paid(request):
    return request.get("dry_run") is False


PAID_RESEARCH_TOOLS = frozenset({"run_portfolio_research", "run_recursive_improvement"})
_MAX_CITATIONS = 50
_MAX_CITATION_EVIDENCE = 100
# Only validated URL leaves are exempted, never source metadata or evidence subtrees.
_CITATION_POINTERS = tuple(f"/sources/{i}/url" for i in range(_MAX_CITATIONS)) + tuple(
    f"/evidence/{i}/sources/{j}/url" for i in range(_MAX_CITATION_EVIDENCE) for j in range(_MAX_CITATIONS)
)


def _validate_public_citation(url):
    invalid = ToolError.internal("the producer returned an invalid public evidence citation")
    if not isinstance(url, str) or len(url) > 2048 or "\\" in url or any(c.isspace() for c in url):
        raise invalid
    try:
        parsed = urlparse(url)
        hostname = (parsed.hostname or "").lower().rstrip(".")
        parsed.port  # Invalid port syntax is not a public citation.
    except ValueError:
        raise invalid from None
    if parsed.scheme not in ("http", "https") or not hostname or parsed.username or parsed.password:
        raise invalid
    if hostname.endswith(("amazonaws.com", "amazonaws.com.cn", ".local", ".localhost", ".internal", ".lan", ".home", ".test", ".invalid")) or hostname == "localhost":
        raise invalid
    try:
        address = ip_address(hostname)
    except ValueError:
        if "." not in hostname or hostname.replace(".", "").isdigit():
            raise invalid
    else:
        if not address.is_global:
            raise invalid


def _validate_sources(sources):
    if not isinstance(sources, list) or len(sources) > _MAX_CITATIONS:
        raise ToolError.internal("the producer returned invalid public evidence sources")
    for source in sources:
        _validate_public_citation(source.get("url") if isinstance(source, dict) else None)


def _authorize_research(invocation, request, tool_fields):
    if not _paid(request):
        return
    if invocation.source != "direct_test" and "researcher" not in invocation.groups:
        raise ToolError(FORBIDDEN, "starting paid research requires an authorized experiment submitter", reason="group_required")
    if request.get("confirmed_by_user") is not True:
        raise ToolError(PRECONDITION_FAILED, "starting paid research requires explicit user confirmation", reason="confirmation_required")


def _validate_reference(doc):
    if "analyses" in doc and "analysis_id" not in doc:
        return
    ref = doc.get("analysis_ref") or {}
    if ref.get("owner") != "financemodel" or ref.get("artifact_id") != doc.get("analysis_id") or ref.get("kind") != "classical_analysis":
        raise ToolError.internal("the producer returned an inconsistent immutable analysis reference")
    _validate_sources(doc.get("sources", []))
    evidence = doc.get("evidence", [])
    if isinstance(evidence, list):
        for index, item in enumerate(evidence):
            if isinstance(item, dict) and "sources" in item:
                if index >= _MAX_CITATION_EVIDENCE:
                    raise ToolError.internal("the producer returned too many citation evidence records")
                _validate_sources(item["sources"])


def _benchmark_category(estimate):
    """Recognize only a typed, matching benchmark proposal, never a free-text exemption."""
    from finplan_contracts.validate import validate

    proposal = estimate.get("proposed_experiment") or {}
    tool = proposal.get("tool_request") or {}
    arguments = tool.get("arguments") or {}
    family = arguments.get("job_type")
    expected = {"swarm_mode_a": ("qwen_swarm", "gpu"), "jev_backtest": ("jev", "cpu_research")}.get(family)
    if not expected or tool.get("name") != "submit_experiment" or proposal.get("job_type") != family:
        return None
    strategy, category = expected
    if arguments.get("purpose") != "research" or arguments.get("dry_run") is not True or (arguments.get("configuration") or {}).get("payload", {}).get("strategy") != strategy:
        return None
    if proposal.get("budget_category") != category or (estimate.get("cost_estimate") or {}).get("budget_category") != category:
        return None
    return category if validate(arguments, "tools/submit-experiment-request").valid else None


def _invoke(ctx, request):
    operation = ctx.spec.name
    down = dict(request)
    if "idempotency_key" in down:
        down = ctx.downstream_body(down)
    if operation in PAID_RESEARCH_TOOLS and _paid(request):
        estimate = producer_doc(ctx.jobs.classical(operation, {**down, "dry_run": True}, ctx.meta), "research estimate")
        if operation == "run_recursive_improvement" and (estimate.get("state") != "awaiting_experiment_approval" or estimate.get("job")):
            # Running/stopped/completed cycles are evidence reads. There is no cost
            # estimate to authorize and no reason to call the mutation path again.
            _validate_reference(estimate)
            return estimate
        cost = producer_doc(estimate.get("cost_estimate"), "research cost estimate")
        check_tool_budget(cost, ctx.limits)
        amount = cost.get("estimated_usd_upper_bound")
        benchmark = operation == "run_recursive_improvement" and _benchmark_category(estimate)
        if not isinstance(amount, (int, float)) or isinstance(amount, bool) or not isfinite(amount) or amount < 0:
            raise ToolError(BUDGET_EXCEEDED, "research estimate must be a finite nonnegative amount")
        if not benchmark and amount > .5:
            raise ToolError(BUDGET_EXCEEDED, "weekly research estimate exceeds the USD 0.50 hard cap", limit_usd=.5)
    doc = producer_doc(ctx.jobs.classical(operation, down, ctx.meta), "classical analysis")
    _validate_reference(doc)
    if operation == "get_classical_analysis" and doc.get("analysis_id") != request["analysis_id"]:
        raise ToolError.internal("the producer returned another analysis than requested")
    return doc


_DESCRIPTIONS = {
    "explain_portfolio_decision": "Explain an immutable PPO or traditional decision_id using its frozen issued inputs. Traditional decisions use objective counterfactuals/Shapley; PPO reports policy diagnostics and truthful attribution limits. Does not rerun or accept a new recommendation.",
    "compare_portfolio_decisions": "Compare previous_decision_id and current_decision_id from durable recommendation history, including input snapshots, holdings revisions, targets and strategy identity. Traditional comparable decisions support grouped Shapley; PPO or cross-family comparisons expose descriptive changes and limitations.",
    "evaluate_portfolio_decision": "Evaluate a stored decision against later approved observed prices, saved paper revisions and simulated fills. Separate daily attribution from the declared strategy objective and horizon; preserve frozen sequential-policy replay, same-cost controls, partial windows and unavailable evidence. A short loss or one realized path does not establish policy failure or optimality. Save discrepancy evidence for recurring review.",
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
    "run_recursive_improvement": "Create or resume a persisted portfolio improvement cycle: review horizon evidence and feedback, propose a sandbox experiment, retrieve job/result lineage and recommend the next bounded iteration. dry_run defaults true; paid launch requires a verified researcher, confirmed_by_user=true and idempotency_key. Ordinary CPU research is capped at USD 0.50/week and USD 2/month; typed Qwen/Jev benchmarks retain their sandbox category limits and compute/vendor approval. At most three iterations, no automatic strategy activation. Returns honest Qwen swarm/Jev capability status and stopping reasons.",
}

for _name, _description in _DESCRIPTIONS.items():
    register_tool(
        _name,
        description=_description,
        list_key="analyses" if _name == "list_classical_analyses" else None,
        grant_pointers=_CITATION_POINTERS,
        writes=_paid if _name in PAID_RESEARCH_TOOLS else None,
        authorize=_authorize_research if _name in PAID_RESEARCH_TOOLS else None,
    )(_invoke)
