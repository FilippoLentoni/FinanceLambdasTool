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

import logging
import random
import time
from typing import Any, Mapping

from ..core.errors import DEPENDENCY_UNAVAILABLE, INTERNAL, NOT_FOUND, RATE_LIMITED, ToolError, from_producer_envelope
from ..core.transport import CallMeta, ProducerRequest, ProducerResponse, Transport

__all__ = ["PlatformClient", "call", "MAX_ATTEMPTS"]

log = logging.getLogger(__name__)

#: Bounded retry of transient producer failures (D7): at most this many sends per call.
MAX_ATTEMPTS = 3
#: Backoff before attempt n+1: BACKOFF_BASE_SECONDS * 2**n plus up to 50% jitter.
BACKOFF_BASE_SECONDS = 0.25
#: Message of the producers' API Gateway 5xx gateway-response templates.
GATEWAY_5XX_MESSAGE = "internal error"
_sleep = time.sleep
_clock = time.monotonic


def _gateway_generated(resp: ProducerResponse, meta: CallMeta) -> bool:
    """A 5xx contract envelope written by the producer's API Gateway (gateway responses
    ``DEFAULT_5XX``/``INTEGRATION_FAILURE``/``INTEGRATION_TIMEOUT``), not by the producer's code:
    the gateway stamps ``$context.requestId``, never the caller's correlation ID. This is what a
    throttled or failed producer Lambda invocation looks like (account concurrency, cold start)."""
    body = resp.body
    if not (resp.status >= 500 and isinstance(body, Mapping) and body.get("code") == INTERNAL and body.get("correlation_id") != meta.correlation_id):
        return False
    return "x-amzn-errortype" in resp.headers or body.get("message") == GATEWAY_5XX_MESSAGE


def _transient(resp: ProducerResponse, meta: CallMeta, *, all_transient: bool) -> bool:
    if _gateway_generated(resp, meta):
        return True
    if not all_transient:
        # Producer-declared throttling/unavailability passes through once (MKT-08): the caller
        # decides when to retry. Only gateway-generated 5xx (a throttled or failed producer
        # Lambda that never ran the request's code path) is retried for every call.
        return False
    if resp.status in (429, 502, 503, 504):
        return True
    if isinstance(resp.body, Mapping) and resp.body.get("code") in (RATE_LIMITED, DEPENDENCY_UNAVAILABLE) and resp.body.get("retryable") is True:
        return True
    return False


def _replay_safe(method: str, body: Any) -> bool:
    """Reads, and writes carrying an idempotency key (the producer replays the first outcome)."""
    return method.upper() == "GET" or (isinstance(body, Mapping) and bool(body.get("idempotency_key")))


def call(transport: Transport | None, producer: str, method: str, path: str, meta: CallMeta, *, body: Any = None, query: Mapping[str, Any] | None = None, timeout: float = 15.0, retry_transient: bool = False) -> Any:
    """Send one request and return the 2xx body, or raise the mapped :class:`ToolError`.

    Gateway-generated 5xx envelopes of replay-safe requests (reads, and writes with an
    ``idempotency_key``) are retried up to :data:`MAX_ATTEMPTS` times with exponential backoff and
    jitter within ``timeout``; one that persists is ``DEPENDENCY_UNAVAILABLE`` (retryable), never a
    bare ``INTERNAL``. With ``retry_transient`` (durable activity archiving, whose failure would
    otherwise turn a successful call into an error) transport errors, throttling and 502/503/504
    are retried the same way.
    """
    if transport is None:
        raise ToolError.dependency_unavailable(f"the {producer} API is not available in this environment", producer=producer)
    deadline = _clock() + timeout
    retry_ok = _replay_safe(method, body)
    attempt = 0
    while True:
        attempt += 1
        remaining = max(0.5, deadline - _clock())
        try:
            resp = transport.send(ProducerRequest(method=method, path=path, query=dict(query or {}), body=body, headers=meta.headers(), timeout_seconds=min(timeout, remaining)))
        except ToolError as exc:
            if not (retry_ok and retry_transient and exc.retryable and exc.code in (DEPENDENCY_UNAVAILABLE, RATE_LIMITED)) or not _backoff(attempt, deadline, producer, path, meta, exc.code):
                raise
            continue
        if resp.ok:
            return resp.body
        if retry_ok and _transient(resp, meta, all_transient=retry_transient) and _backoff(attempt, deadline, producer, path, meta, resp.status):
            continue
        break
    if _gateway_generated(resp, meta):
        log.warning("%s API gateway returned %s for %s %s after %d attempt(s) (correlation_id=%s gateway_request_id=%s)", producer, resp.status, method, path, attempt, meta.correlation_id, resp.body.get("correlation_id"))
        raise ToolError(DEPENDENCY_UNAVAILABLE, f"the {producer} API is temporarily unavailable", retryable=True, producer=producer, reason="producer_gateway_error", producer_status=resp.status, attempts=attempt)
    err = from_producer_envelope(resp.body, resp.status)
    if err.code == NOT_FOUND and isinstance(resp.body, Mapping) and (resp.body.get("details") or {}).get("record_type") == "route":
        raise ToolError(DEPENDENCY_UNAVAILABLE, f"the {producer} release in this environment does not serve this route", producer=producer, reason="route_not_deployed")
    if err.code == INTERNAL:
        log.warning("%s API returned INTERNAL (status %s) for %s %s (correlation_id=%s attempts=%d)", producer, resp.status, method, path, meta.correlation_id, attempt)
    raise err


def _backoff(attempt: int, deadline: float, producer: str, path: str, meta: CallMeta, what: Any) -> bool:
    """Sleep before the next attempt; False when attempts or the deadline are exhausted."""
    if attempt >= MAX_ATTEMPTS:
        return False
    delay = BACKOFF_BASE_SECONDS * (2 ** (attempt - 1)) * (1 + random.random() / 2)  # noqa: S311 - jitter only
    if _clock() + delay + 0.5 > deadline:
        return False
    log.info("retrying %s %s after transient %s (attempt %d/%d, correlation_id=%s)", producer, path, what, attempt + 1, MAX_ATTEMPTS, meta.correlation_id)
    _sleep(delay)
    return True


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

    def get_publication(self, publication_id, meta):
        return self._get(f"v1/publications/{publication_id}",meta)

    def list_publications(self, plan_id, meta, **query):
        return self._get(f"v1/plans/{plan_id}/publications",meta,query)

    def list_executions(self, publication_id, meta, **query):
        return self._get(f"v1/publications/{publication_id}/executions",meta,query)

    # Versioned paper decision and activity lifecycle (contracts 1.5).
    def get_portfolio_history(self, portfolio_id, meta, **query):
        return self._get(f"v1/portfolios/{portfolio_id}/history", meta, query)

    def list_portfolio_decisions(self, portfolio_id, meta, **query):
        return self._get(f"v1/portfolios/{portfolio_id}/decisions", meta, query)

    def get_portfolio_decision(self, decision_id, meta):
        return self._get(f"v1/portfolio-decisions/{decision_id}", meta)

    def resolve_portfolio_decision(self, decision_id, body, meta):
        return self._post(f"v1/portfolio-decisions/{decision_id}/resolution", meta, dict(body))

    def list_market_snapshots(self, meta, **query):
        return self._get("v1/snapshots", meta, query)

    def record_agent_activity(self, body, meta):
        # Idempotent archive write: retried on every transient failure (D7: concurrent sessions).
        return call(self.plan, self.PRODUCER, "POST", "v1/activity-events", meta, body=dict(body), timeout=self.timeout, retry_transient=True)

    def list_agent_activity(self, meta, **query):
        return self._get("v1/activity-events", meta, query)
