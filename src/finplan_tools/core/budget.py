"""Per-call tool budget check by budget category (D5; EXP-05).

The tool compares FinanceModel's dry-run estimate (``cost_estimate``: ``estimated_usd_upper_bound``,
``budget_category``) with the static ``tool-limits.max_estimated_usd_per_call`` map. A category
missing from the map, or an estimate above its limit, is ``BUDGET_EXCEEDED`` with the estimate,
category and limit in ``details``. FinanceModel's category pre-flight and the USD 50 AWS Budgets
deny stay authoritative; this check only stops a single tool call from spending more than its
configured share. The tool never approves a run and never reads ``budget-state``.
"""

from __future__ import annotations

from typing import Any, Mapping

from .config import ToolLimits
from .errors import BUDGET_EXCEEDED, ToolError

__all__ = ["check_tool_budget"]


def check_tool_budget(cost_estimate: Mapping[str, Any], limits: ToolLimits) -> None:
    """Raise ``BUDGET_EXCEEDED`` unless the estimate is within its category's per-call limit."""
    category = cost_estimate.get("budget_category")
    estimate = cost_estimate.get("estimated_usd_upper_bound")
    if not isinstance(category, str) or isinstance(estimate, bool) or not isinstance(estimate, (int, float)):
        raise ToolError.internal("the producer estimate has no budget category or amount")
    limit = limits.per_call_limit(category)
    details = {"estimated_usd_upper_bound": float(estimate), "budget_category": category, "limit_usd": limit, "limit_source": "tool-limits.max_estimated_usd_per_call"}
    if limit is None:
        raise ToolError(BUDGET_EXCEEDED, f"no per-call tool limit is configured for budget category {category}", details=details)
    if float(estimate) > limit + 1e-9:
        raise ToolError(BUDGET_EXCEEDED, f"the estimate exceeds the per-call tool limit for {category}", details=details)
