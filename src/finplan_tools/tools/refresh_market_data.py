"""``refresh_market_data`` (spec market-data-tools; tasks 5.1, 5.2, 5.2a).

Exactly one call to the platform ingestion operation (``POST`` at the same-environment
``/finplan/<env>/financialplanning/api/ingestion-endpoint``) with the derived idempotency key. The
tool never calls a market-data provider, imports no provider library and reads no provider secret:
the platform's provider adapter (fixture provider in phase 1, ``yfinance`` behind the adapter in
phase 2) does the retrieval, validation and storage.

Outcomes are the platform's, unchanged: the snapshot with its lineage and quality flags (including
``missing_sessions`` / ``empty_response`` for partial or empty provider answers, with
``coverage_complete`` as reported), a capability rejection (``VALIDATION_FAILED``), provider
throttling (``RATE_LIMITED`` / ``DEPENDENCY_UNAVAILABLE`` with the platform's ``retryable``), and
``BUDGET_EXCEEDED`` (always ``retryable`` false). The tool adds no retries of its own.
"""

from __future__ import annotations

from typing import Any

from ..core.errors import BUDGET_EXCEEDED, ToolError
from ..core.registry import register_tool
from ._common import producer_doc, project

RESPONSE = "tools/refresh-market-data-response"


@register_tool(
    "refresh_market_data",
    description=(
        "Ask the platform to ingest daily completed observations for a configured dataset such as the "
        "S&P 500 tracking-ETF series finance/etf-daily/<instrument> over a date range. Returns the "
        "platform-minted input_snapshot_id, coverage, lineage and quality flags exactly as the platform "
        "recorded them. Requires an idempotency_key; a repeat returns the original snapshot."
    ),
)
def refresh_market_data(ctx: Any, request: dict[str, Any]) -> dict[str, Any]:
    body = ctx.downstream_body(request)
    try:
        result = producer_doc(ctx.platform.run_ingestion(body, ctx.meta), "ingestion result")
    except ToolError as err:
        if err.code == BUDGET_EXCEEDED and err.retryable:
            raise ToolError(BUDGET_EXCEEDED, err.message, retryable=False, details=err.details) from None
        raise
    doc = project(result, RESPONSE)
    if request.get("synthetic") is True or result.get("synthetic") is True:
        doc["synthetic"] = True
    ctx.record_ids(doc)
    return doc
