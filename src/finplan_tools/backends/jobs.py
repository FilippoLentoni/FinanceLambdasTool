"""Typed client for the FinanceModel job API (``/finplan/<env>/financemodel/api/job-endpoint``).

Only ``submit_job`` (with ``dry_run``), ``get_job_status`` and ``get_job_result``. There is
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

    def __init__(self, transport: Transport | None, *, timeout: float = 15.0) -> None:
        self.transport = transport
        self.timeout = timeout

    def submit_job(self, body: Mapping[str, Any], meta: CallMeta) -> Any:
        """``POST /v1/jobs``; ``body['dry_run']`` true returns an estimate and no ``run_id``."""
        return call(self.transport, self.PRODUCER, "POST", "v1/jobs", meta, body=dict(body), timeout=self.timeout)

    def get_job_status(self, run_id: str, meta: CallMeta) -> Any:
        return call(self.transport, self.PRODUCER, "GET", f"v1/jobs/{run_id}", meta, timeout=self.timeout)

    def get_job_result(self, run_id: str, meta: CallMeta) -> Any:
        return call(self.transport, self.PRODUCER, "GET", f"v1/jobs/{run_id}/result", meta, timeout=self.timeout)
