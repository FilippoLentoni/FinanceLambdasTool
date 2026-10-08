"""Phase 1 synthetic-only guard for the plan write tools (design D6; PLN-09).

Before any write, the tool reads the plan and its portfolio through the platform and refuses a
portfolio that is not flagged ``synthetic`` true (``OPERATION_NOT_PERMITTED``, no write). The
platform enforces the same rule; this is defense in depth, so a misconfigured platform never turns a
tool call into a write on a real portfolio.
"""

from __future__ import annotations

from typing import Any

from ..core.errors import OPERATION_NOT_PERMITTED, ToolError
from ._common import producer_doc


def require_synthetic_plan(ctx: Any, plan_id: str) -> dict[str, Any]:
    """Return the platform plan document after checking its portfolio is synthetic."""
    body = producer_doc(ctx.platform.get_plan(plan_id, ctx.meta), "plan")
    plan = producer_doc(body.get("plan"), "plan")
    if plan.get("plan_id") != plan_id:
        raise ToolError.internal("the platform returned another plan than requested")
    portfolio = producer_doc(ctx.platform.get_portfolio(str(plan.get("portfolio_id")), ctx.meta), "portfolio")
    if portfolio.get("synthetic") is not True:
        raise ToolError(OPERATION_NOT_PERMITTED, "phase 1 plan tools write only to synthetic portfolios", reason="non_synthetic_portfolio", plan_id=plan_id)
    return plan
