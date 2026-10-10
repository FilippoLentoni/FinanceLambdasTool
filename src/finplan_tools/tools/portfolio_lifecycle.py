"""Versioned paper decisions and durable history, shared by both remote MCP gateways."""
from __future__ import annotations

from ..core.errors import FORBIDDEN, PRECONDITION_FAILED, ToolError
from ..core.registry import register_tool
from ._common import producer_doc, project

DATASET_ID = "finance/equity-etf-daily/research-universe"


def _portfolio(ctx, request):
    if request.get("portfolio_id"):
        return request["portfolio_id"]
    refs = ctx.runtime.references
    plan_id = refs.get("financialplanning", "config", "research-plan-ref") if refs else None
    if not plan_id:
        raise ToolError(PRECONDITION_FAILED, "the default saved paper portfolio is not configured", reason="paper_portfolio_not_configured")
    plan = producer_doc(ctx.platform.get_plan(plan_id, ctx.meta), "default plan").get("plan", {})
    portfolio_id = plan.get("portfolio_id")
    if not portfolio_id:
        raise ToolError.internal("the default plan has no portfolio reference")
    return portfolio_id


def _page(request):
    return {"page_size": request.get("limit", 3), **({"next_token": request["next_token"]} if request.get("next_token") else {})}


def _authorize_resolution(invocation, request, tool_fields):
    # The shared pipeline replaces identity/groups only after signature verification.
    if invocation.source != "gateway" or not invocation.identity.startswith("cognito:") or not invocation.groups.intersection({"viewer", "researcher", "plan_editor", "plan_publisher"}):
        raise ToolError(FORBIDDEN, "paper acceptance or rejection requires a verified human user", reason="verified_user_token_required")
    if request.get("confirmed_by_user") is not True:
        raise ToolError(PRECONDITION_FAILED, "review and confirm this paper decision before recording your choice", reason="confirmation_required")


@register_tool("get_portfolio_history", description="Retrieve immutable paper holdings revisions, newest first (default latest three for the saved portfolio). Each revision links its accepted decision and approved market snapshot. Does not change holdings.", list_key="history", truncated_key=None)
def get_portfolio_history(ctx, request):
    return producer_doc(ctx.platform.get_portfolio_history(_portfolio(ctx, request), ctx.meta, **_page(request)), "portfolio history")


@register_tool("list_portfolio_decisions", description="Retrieve issued paper recommendations for PPO and traditional strategies, newest first (default latest three for the saved portfolio), including acceptance/rejection status and snapshot/revision references.", list_key="decisions", truncated_key=None)
def list_portfolio_decisions(ctx, request):
    return producer_doc(ctx.platform.list_portfolio_decisions(_portfolio(ctx, request), ctx.meta, **_page(request)), "portfolio decisions")


@register_tool("get_portfolio_decision", description="Retrieve the exact immutable recommendation issued under decision_id, with holdings, market snapshot, strategy provenance and the recorded human resolution. Use it before explaining or accepting a prior recommendation.")
def get_portfolio_decision(ctx, request):
    return producer_doc(ctx.platform.get_portfolio_decision(request["decision_id"], ctx.meta), "portfolio decision")


@register_tool("resolve_portfolio_decision", description="After explicit human review, accept or reject a stored recommendation using decision_id, expected_revision, confirmed_by_user=true and idempotency_key. Acceptance atomically saves simulated fractional fills at the issued reference prices, costs and a new paper holdings revision; rejection leaves holdings unchanged. Requires the caller's verified user token. Never places broker orders.", authorize=_authorize_resolution)
def resolve_portfolio_decision(ctx, request):
    body = ctx.downstream_body({k: v for k, v in request.items() if k not in ("decision_id", "portfolio_id")})
    return producer_doc(ctx.platform.resolve_portfolio_decision(request["decision_id"], body, ctx.meta), "paper decision resolution")


@register_tool("list_market_snapshots", description="List immutable stored approved market snapshots, newest first (default latest three for the equity/ETF research universe). These are pre-ingested completed daily observations, not on-demand downloads.", list_key="snapshots", truncated_key=None)
def list_market_snapshots(ctx, request):
    return producer_doc(ctx.platform.list_market_snapshots(ctx.meta, dataset_id=request.get("dataset_id", DATASET_ID), **_page(request)), "market snapshots")


@register_tool("record_agent_activity", description="Archive a completed agent turn or investigation receipt with narrative, versioned skills, sanitized tool request/result references and correlation/session identifiers. Evidence storage only; does not approve a recommendation or change holdings.")
def record_agent_activity(ctx, request):
    from ..core.activity import sanitize
    return producer_doc(ctx.platform.record_agent_activity(ctx.downstream_body(sanitize(request)), ctx.meta), "agent activity")


@register_tool("list_agent_activity", description="Retrieve durable agent-turn and tool invocation receipts for postmortem analysis, filtered by portfolio or session. Includes sanitized successful and failed tool inputs/results and exact skill versions.", list_key="events", truncated_key=None, grant_pointers=("/events",))
def list_agent_activity(ctx, request):
    query = _page(request)
    for key in ("portfolio_id", "session_id"):
        if request.get(key):
            query[key] = request[key]
    from ..core.activity import sanitize
    return sanitize(producer_doc(ctx.platform.list_agent_activity(ctx.meta, **query), "agent activity history"))
