"""Typed client for the FinanceModel job API (``/finplan/<env>/financemodel/api/job-endpoint``).

Only ``submit_job`` (with ``dry_run``), ``get_job_status``, ``get_job_result`` and (contracts 1.1.0,
``production_strategy`` tool) the production-strategy selection operations
``GET /v1/production-strategy`` and ``PUT /v1/production-strategy`` (body ``action`` ``set`` or
``clear``, ``confirmed_by_user`` and the derived ``idempotency_key``). There is
deliberately no ``approve_run``, ``cancel_job`` or ``list_jobs`` method: tools never approve or
cancel runs (EXP-07; a static test asserts no such method or route string exists in the package).
FinanceModel is the only minter of ``run_id`` and the authority on job budgets and approvals.
"""

from __future__ import annotations

from typing import Any, Mapping

from ..core.transport import CallMeta, Transport
from .platform import call

__all__ = ["JobClient"]


class JobClient:
    PRODUCER = "financemodel"

    def __init__(self, transport: Transport | None, *, timeout: float = 15.0, strategy_client=None) -> None:
        self.transport = transport
        self.timeout = timeout
        self.strategy_client = strategy_client

    def submit_job(self, body: Mapping[str, Any], meta: CallMeta) -> Any:
        """``POST /v1/jobs``; ``body['dry_run']`` true returns an estimate and no ``run_id``."""
        return call(self.transport, self.PRODUCER, "POST", "v1/jobs", meta, body=dict(body), timeout=self.timeout)

    def get_job_status(self, run_id: str, meta: CallMeta) -> Any:
        return call(self.transport, self.PRODUCER, "GET", f"v1/jobs/{run_id}", meta, timeout=self.timeout)

    def get_job_result(self, run_id: str, meta: CallMeta) -> Any:
        return call(self.transport, self.PRODUCER, "GET", f"v1/jobs/{run_id}/result", meta, timeout=self.timeout)

    # ---------------------------------------------------------------- production strategy (1.1.0)
    def get_production_strategy(self, meta: CallMeta) -> Any:
        """``GET /v1/production-strategy``: the current selection (``strategy`` null when none)."""
        return call(self.transport, self.PRODUCER, "GET", "v1/production-strategy", meta, timeout=self.timeout)

    def put_production_strategy(self, body: Mapping[str, Any], meta: CallMeta) -> Any:
        """``PUT /v1/production-strategy`` with ``action`` ``set`` or ``clear`` (FinanceModel's
        selection operation, the single writer of its production-strategy key)."""
        return call(self.transport, self.PRODUCER, "PUT", "v1/production-strategy", meta, body=dict(body), timeout=self.timeout)

    def recommend_portfolio(self, body, meta):
        from ..core.errors import ToolError
        if self.strategy_client is None:
            raise ToolError.dependency_unavailable("the selected-strategy service is unavailable")
        return self.strategy_client.recommend(body, meta)

    def get_performance_evidence(self, body, meta):
        import json
        return call(self.transport,self.PRODUCER,"GET","v1/performance-evidence",meta,query={"request":json.dumps(dict(body),separators=(",",":"))},timeout=self.timeout)
