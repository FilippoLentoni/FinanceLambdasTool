"""Typed client for the FinancialPlanning plan and ingestion APIs (D1 route table).

Only the routes the tools need, and only the ones their role classes may call. There is no method
for execution routes, imports/exports or staged-output acceptance: the tools never call them.

=====================================  ======================================  ===========
method                                 route                                   role class
=====================================  ======================================  ===========
:meth:`get_plan`                       ``GET /v1/plans/{plan_id}``             reader
:meth:`list_plan_versions`             ``GET /v1/plans/{plan_id}/versions``    reader
:meth:`get_plan_version`               ``GET /v1/plan-versions/{id}``          reader
:meth:`get_portfolio`                  ``GET /v1/portfolios/{id}``             reader
:meth:`get_snapshot`                   ``GET /v1/snapshots/{id}``              reader
:meth:`read_snapshot_observations`     ``GET /v1/snapshots/{id}/observations`` reader
:meth:`create_plan_version`            ``POST /v1/plans/{plan_id}/versions``   plan-writer
:meth:`validate_plan_version`          ``POST /v1/plan-versions/{id}/validate`` plan-writer
:meth:`publish_plan_version`           ``POST /v1/plans/{plan_id}/publications`` plan-writer
:meth:`run_ingestion`                  ``POST`` ingestion endpoint             submitter
=====================================  ======================================  ===========

Errors: a producer envelope keeps its code and ``retryable``; a ``NOT_FOUND`` for an unknown
*route* (an older platform release) becomes ``DEPENDENCY_UNAVAILABLE``.
"""

from __future__ import annotations

from typing import Any, Mapping

from ..core.errors import DEPENDENCY_UNAVAILABLE, NOT_FOUND, ToolError, from_producer_envelope
from ..core.transport import CallMeta, ProducerRequest, Transport

__all__ = ["PlatformClient", "call"]


def call(transport: Transport | None, producer: str, method: str, path: str, meta: CallMeta, *, body: Any = None, query: Mapping[str, Any] | None = None, timeout: float = 15.0) -> Any:
    """Send one request and return the 2xx body, or raise the mapped :class:`ToolError`."""
    if transport is None:
        raise ToolError.dependency_unavailable(f"the {producer} API is not available in this environment", producer=producer)
    resp = transport.send(ProducerRequest(method=method, path=path, query=dict(query or {}), body=body, headers=meta.headers(), timeout_seconds=timeout))
    if resp.ok:
        return resp.body
    err = from_producer_envelope(resp.body, resp.status)
    if err.code == NOT_FOUND and isinstance(resp.body, Mapping) and (resp.body.get("details") or {}).get("record_type") == "route":
        raise ToolError(DEPENDENCY_UNAVAILABLE, f"the {producer} release in this environment does not serve this route", producer=producer, reason="route_not_deployed")
    raise err


class PlatformClient:
    PRODUCER = "financialplanning"

    def __init__(self, plan: Transport | None, ingestion: Transport | None = None, *, timeout: float = 15.0) -> None:
        self.plan = plan
        self.ingestion = ingestion
        self.timeout = timeout

    def _get(self, path: str, meta: CallMeta, query: Mapping[str, Any] | None = None) -> Any:
        return call(self.plan, self.PRODUCER, "GET", path, meta, query=query, timeout=self.timeout)

    def _post(self, path: str, meta: CallMeta, body: Any) -> Any:
        return call(self.plan, self.PRODUCER, "POST", path, meta, body=body, timeout=self.timeout)

    # ---------------------------------------------------------------- reads
    def get_plan(self, plan_id: str, meta: CallMeta) -> Any:
        return self._get(f"v1/plans/{plan_id}", meta)

    def list_plan_versions(self, plan_id: str, meta: CallMeta, *, page_size: int | None = None, next_token: str | None = None) -> Any:
        return self._get(f"v1/plans/{plan_id}/versions", meta, {"page_size": page_size, "next_token": next_token})

    def get_plan_version(self, plan_version_id: str, meta: CallMeta) -> Any:
        return self._get(f"v1/plan-versions/{plan_version_id}", meta)

    def get_portfolio(self, portfolio_id: str, meta: CallMeta) -> Any:
        return self._get(f"v1/portfolios/{portfolio_id}", meta)

    def get_snapshot(self, input_snapshot_id: str, meta: CallMeta) -> Any:
        return self._get(f"v1/snapshots/{input_snapshot_id}", meta)

    def read_snapshot_observations(self, input_snapshot_id: str, meta: CallMeta, query: Mapping[str, Any] | None = None) -> Any:
        return self._get(f"v1/snapshots/{input_snapshot_id}/observations", meta, query)

    # ---------------------------------------------------------------- writes
    def create_plan_version(self, plan_id: str, body: Mapping[str, Any], meta: CallMeta) -> Any:
        return self._post(f"v1/plans/{plan_id}/versions", meta, dict(body))

    def validate_plan_version(self, plan_version_id: str, body: Mapping[str, Any], meta: CallMeta) -> Any:
        return self._post(f"v1/plan-versions/{plan_version_id}/validate", meta, dict(body))

    def publish_plan_version(self, plan_id: str, body: Mapping[str, Any], meta: CallMeta, ) -> Any:
        return self._post(f"v1/plans/{plan_id}/publications", meta, dict(body))

    def run_ingestion(self, body: Mapping[str, Any], meta: CallMeta, *, timeout: float | None = None) -> Any:
        # The ingestion reference is the full POST URL (``.../v1/ingestions``): empty path.
        return call(self.ingestion, self.PRODUCER, "POST", "", meta, body=dict(body), timeout=timeout or self.timeout)
