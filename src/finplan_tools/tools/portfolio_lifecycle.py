"""Versioned paper decisions and durable history, shared by both remote MCP gateways."""
from __future__ import annotations

import base64
import hashlib
import json

from ..core.bounds import response_size
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


def _history_page(ctx, request, list_key, read, label, oversized=None):
    """Return native, partition-bound producer cursors even when the byte cap cuts a page.

    Re-reading a producer page by numeric offset is unsafe when newer entries arrive.
    On overflow, read individual records and retain the producer cursor immediately
    after the last included record. The shared pipeline then has nothing left to cut.
    """
    token = request.get("next_token")
    if token:
        try:
            state = json.loads(base64.urlsafe_b64decode(token + "=" * (-len(token) % 4)))
        except (ValueError, TypeError):
            state = None  # The producer validates malformed native tokens.
        if isinstance(state, dict) and "v" in state and "t" in state and "e" in state:
            raise ToolError.validation("restart this history query without the obsolete continuation token", pointer="/next_token")
    size = request.get("limit", 3)

    def fetch(page_size, cursor):
        query = {"page_size": page_size, **({"next_token": cursor} if cursor else {})}
        return producer_doc(read(**query), label)

    page = fetch(size, token)
    if response_size(page) <= ctx.limits.response_max_bytes:
        return page
    entries = []
    while len(entries) < size:
        single = fetch(1, token)
        rows = single.get(list_key) or []
        if len(rows) > 1:
            raise ToolError.internal("the producer did not honor the history page size")
        candidate = {**single, list_key: entries + rows}
        if response_size(candidate) > ctx.limits.response_max_bytes:
            if not entries:
                if oversized and len(rows) == 1:
                    return oversized(single)
                raise ToolError.internal("one history record exceeds the response size limit", reason="history_record_too_large")
            return {**single, list_key: entries, "next_token": token}
        entries.extend(rows)
        next_token = single.get("next_token")
        if not next_token or not rows:
            return candidate
        if next_token == token:
            raise ToolError.internal("the history producer did not advance its continuation token")
        token = next_token
    return candidate


def _encode_cursor(prefix, state):
    return prefix + base64.urlsafe_b64encode(json.dumps(state, sort_keys=True, separators=(",", ":")).encode()).decode().rstrip("=")


def _decode_cursor(prefix, token):
    try:
        raw = token[len(prefix):]
        state = json.loads(base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)))
        if not isinstance(state, dict):
            raise ValueError
        return state
    except (ValueError, TypeError):
        raise ToolError.validation("invalid activity fragment continuation token", pointer="/next_token") from None


def _activity_fragment(ctx, page, partition, *, offset=0, expected=None):
    """Expose a large immutable payload in verifiable UTF-8 canonical JSON fragments."""
    events = page.get("events") or []
    if len(events) != 1:
        raise ToolError.internal("activity fragment lookup did not return one immutable event")
    event = events[0]
    raw = json.dumps(event["payload"], sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    checksum = "sha256:" + hashlib.sha256(raw).hexdigest()
    after = page.get("next_token")
    if expected is not None:
        if event.get("activity_event_id") != expected.get("a") or event.get("checksum") != expected.get("c") or checksum != expected.get("h"):
            raise ToolError("CONFLICT", "the immutable activity evidence no longer matches its continuation token")
        after = expected.get("p")
    if isinstance(offset, bool) or not isinstance(offset, int) or not 0 <= offset < len(raw):
        raise ToolError.validation("invalid activity fragment offset", pointer="/next_token")
    try:
        remaining = raw[offset:].decode("utf-8")
    except UnicodeDecodeError:
        raise ToolError.validation("activity fragment offset is not a UTF-8 boundary", pointer="/next_token") from None
    state = {"v": 1, "e": ctx.environment, "f": partition, "a": event["activity_event_id"], "c": event["checksum"], "h": checksum, "p": after}
    lo, hi, best = 1, len(remaining), None
    while lo <= hi:
        mid = (lo + hi) // 2
        fragment = remaining[:mid]
        end = offset + len(fragment.encode("utf-8"))
        next_token = _encode_cursor("ac1_", {**state, "o": end}) if end < len(raw) else after
        payload = {"representation": "chunked_immutable_json", "encoding": "utf-8", "format": "canonical-json", "fragment": fragment,
                   "offset": offset, "end_offset": end, "total": len(raw), "payload_checksum": checksum}
        candidate = {**page, "events": [{**event, "payload": payload}], "next_token": next_token}
        if response_size(candidate) <= ctx.limits.response_max_bytes:
            best, lo = candidate, mid + 1
        else:
            hi = mid - 1
    if best is None:
        raise ToolError.internal("activity metadata exceeds the response size limit", reason="history_record_too_large")
    return best


def _authorize_resolution(invocation, request, tool_fields):
    # The shared pipeline replaces identity/groups only after signature verification.
    if invocation.source != "gateway" or not invocation.identity.startswith("cognito:") or not invocation.groups.intersection({"viewer", "researcher", "plan_editor", "plan_publisher"}):
        raise ToolError(FORBIDDEN, "paper acceptance or rejection requires a verified human user", reason="verified_user_token_required")
    if request.get("confirmed_by_user") is not True:
        raise ToolError(PRECONDITION_FAILED, "review and confirm this paper decision before recording your choice", reason="confirmation_required")


@register_tool("get_portfolio_history", description="Retrieve immutable paper holdings revisions, newest first (default latest three for the saved portfolio). Each revision links its accepted decision and approved market snapshot. Does not change holdings.", list_key="history", truncated_key=None)
def get_portfolio_history(ctx, request):
    portfolio_id = _portfolio(ctx, request)
    return _history_page(ctx, request, "history", lambda **query: ctx.platform.get_portfolio_history(portfolio_id, ctx.meta, **query), "portfolio history")


@register_tool("list_portfolio_decisions", description="Retrieve issued paper recommendations for PPO and traditional strategies, newest first (default latest three for the saved portfolio), including acceptance/rejection status and snapshot/revision references.", list_key="decisions", truncated_key=None)
def list_portfolio_decisions(ctx, request):
    portfolio_id = _portfolio(ctx, request)
    return _history_page(ctx, request, "decisions", lambda **query: ctx.platform.list_portfolio_decisions(portfolio_id, ctx.meta, **query), "portfolio decisions")


@register_tool("get_portfolio_decision", description="Retrieve the exact immutable recommendation issued under decision_id, with holdings, market snapshot, strategy provenance and the recorded human resolution. Use it before explaining or accepting a prior recommendation.")
def get_portfolio_decision(ctx, request):
    return producer_doc(ctx.platform.get_portfolio_decision(request["decision_id"], ctx.meta), "portfolio decision")


@register_tool("resolve_portfolio_decision", description="After explicit human review, accept or reject a stored recommendation using decision_id, expected_revision, confirmed_by_user=true and idempotency_key. Acceptance atomically saves simulated fractional fills at the issued reference prices, costs and a new paper holdings revision; rejection leaves holdings unchanged. Requires the caller's verified user token. Never places broker orders.", authorize=_authorize_resolution)
def resolve_portfolio_decision(ctx, request):
    body = ctx.downstream_body({k: v for k, v in request.items() if k not in ("decision_id", "portfolio_id")})
    return producer_doc(ctx.platform.resolve_portfolio_decision(request["decision_id"], body, ctx.meta), "paper decision resolution")


@register_tool("list_market_snapshots", description="List immutable stored approved market snapshots, newest first (default latest three for the equity/ETF research universe). These are pre-ingested completed daily observations, not on-demand downloads.", list_key="snapshots", truncated_key=None)
def list_market_snapshots(ctx, request):
    return _history_page(ctx, request, "snapshots", lambda **query: ctx.platform.list_market_snapshots(ctx.meta, dataset_id=request.get("dataset_id", DATASET_ID), **query), "market snapshots")


@register_tool("record_agent_activity", description="Archive a completed agent turn or investigation receipt with narrative, versioned skills, sanitized tool request/result references and correlation/session identifiers. Evidence storage only; does not approve a recommendation or change holdings.")
def record_agent_activity(ctx, request):
    from ..core.activity import sanitize
    return producer_doc(ctx.platform.record_agent_activity(ctx.downstream_body(sanitize(request)), ctx.meta), "agent activity")


@register_tool("list_agent_activity", description="Retrieve durable agent-turn and tool invocation receipts for postmortem analysis, filtered by portfolio or session. Includes sanitized successful and failed tool inputs/results and exact skill versions. Oversized payloads are marked canonical-JSON fragments: follow next_token, concatenate fragments by UTF-8 byte offset, and verify payload_checksum before interpreting the reconstructed evidence.", list_key="events", truncated_key=None, grant_pointers=("/events",))
def list_agent_activity(ctx, request):
    query = {}
    for key in ("portfolio_id", "session_id"):
        if request.get(key):
            query[key] = request[key]
    if not request.get("portfolio_id") and not request.get("session_id"):
        query["portfolio_id"] = _portfolio(ctx, request)
    from ..core.activity import sanitize
    token = request.get("next_token", "")
    if token.startswith("ac1_"):
        state = _decode_cursor("ac1_", token)
        if state.get("v") != 1 or state.get("e") != ctx.environment or state.get("f") != query:
            raise ToolError.validation("activity continuation token belongs to another environment or history", pointer="/next_token")
        exact = _encode_cursor("ae1_", {"activity_event_id": state.get("a")})
        page = sanitize(producer_doc(ctx.platform.list_agent_activity(ctx.meta, **query, page_size=1, next_token=exact), "immutable activity event"))
        return _activity_fragment(ctx, page, query, offset=state.get("o"), expected=state)
    return _history_page(ctx, request, "events", lambda **page: sanitize(producer_doc(ctx.platform.list_agent_activity(ctx.meta, **query, **page), "agent activity history")), "agent activity history",
                         oversized=lambda page: _activity_fragment(ctx, page, query))
