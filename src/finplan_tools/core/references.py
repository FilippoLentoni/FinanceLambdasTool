"""Same-environment producer references from SSM, with an in-memory TTL cache (D2, D9; TRH-11).

Only references are cached: endpoints, release manifests and configuration, keyed by parameter
name, for ``reference_ttl_seconds`` (default 300). Producer *data* (plan heads, job status,
publication state) is never cached anywhere in this package, so a head that moved between two
calls is always read fresh from the producer.

Every name is checked with the contract ``finplan_contracts.ssm.check_read`` rule: a tool reads
only ``/finplan/<own env>/...`` and ``/finplan/shared/...`` (ENVW-01). An absent parameter resolves
to ``None`` (the caller turns that into ``DEPENDENCY_UNAVAILABLE``); the absence is cached for the
same TTL, so a producer release that appears later is picked up within one TTL without a rebuild.
"""

from __future__ import annotations

import json
import threading
import time
from typing import Any, Callable

from finplan_contracts import ssm as contract_ssm

from .config import REPO

__all__ = ["ReferenceResolver", "ReferenceError_", "PLAN_ENDPOINT", "INGESTION_ENDPOINT", "JOB_ENDPOINT"]

PLAN_ENDPOINT = ("financialplanning", "api", "plan-endpoint")
INGESTION_ENDPOINT = ("financialplanning", "api", "ingestion-endpoint")
JOB_ENDPOINT = ("financemodel", "api", "job-endpoint")
PLATFORM_MANIFEST = ("financialplanning", "release", "manifest")
MODEL_MANIFEST = ("financemodel", "release", "manifest")
TOOL_LIMITS = (REPO, "config", "tool-limits")
GATEWAY_PRINCIPAL_REF = ("financeagent", "agent", "gateway-principal-ref")
_MISSING = object()


class ReferenceError_(RuntimeError):
    """A reference could not be read (not the same as absent): permission or SSM failure."""


def _is_not_found(exc: Exception) -> bool:
    code = getattr(exc, "response", {}).get("Error", {}).get("Code") if hasattr(exc, "response") else None
    return code == "ParameterNotFound" or type(exc).__name__ == "ParameterNotFound"


class ReferenceResolver:
    """Reads ``/finplan/<env>/<repo>/<category>/<name>`` parameters for one environment."""

    def __init__(self, ssm_client: Any, environment: str, *, ttl_seconds: float = 300, clock: Callable[[], float] = time.monotonic) -> None:
        self._ssm = ssm_client
        self.environment = environment
        self.ttl_seconds = ttl_seconds
        self._clock = clock
        self._cache: dict[str, tuple[float, Any]] = {}
        self._lock = threading.Lock()

    def name(self, repo: str, category: str, name: str, *, shared: bool = False) -> str:
        path = contract_ssm.build(contract_ssm.SHARED if shared else self.environment, repo, category, name)
        decision = contract_ssm.check_read(path, self.environment)
        if not decision.allowed:  # pragma: no cover - build() only yields own-env or shared names
            raise ReferenceError_("; ".join(decision.reasons))
        return path

    def read(self, path: str) -> str | None:
        """Raw value of ``path`` (own environment or shared only); None when absent."""
        decision = contract_ssm.check_read(path, self.environment)
        if not decision.allowed:
            raise ReferenceError_("refused: " + "; ".join(decision.reasons))
        now = self._clock()
        with self._lock:
            hit = self._cache.get(path)
            if hit is not None and now - hit[0] < self.ttl_seconds:
                return None if hit[1] is _MISSING else hit[1]
        try:
            value: Any = self._ssm.get_parameter(Name=path)["Parameter"]["Value"]
        except Exception as exc:  # noqa: BLE001 - classify below
            if not _is_not_found(exc):
                raise ReferenceError_(f"cannot read a reference ({type(exc).__name__})") from None
            value = _MISSING
        with self._lock:
            self._cache[path] = (now, value)
        return None if value is _MISSING else value

    def get(self, repo: str, category: str, name: str, *, shared: bool = False) -> str | None:
        return self.read(self.name(repo, category, name, shared=shared))

    def get_json(self, repo: str, category: str, name: str, *, shared: bool = False) -> Any:
        raw = self.get(repo, category, name, shared=shared)
        if raw is None:
            return None
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            raise ReferenceError_(f"reference {repo}/{category}/{name} is not JSON") from None

    def invalidate(self) -> None:
        with self._lock:
            self._cache.clear()

    # ---------------------------------------------------------------- shortcuts
    def plan_endpoint(self) -> str | None:
        return self.get(*PLAN_ENDPOINT)

    def ingestion_endpoint(self) -> str | None:
        return self.get(*INGESTION_ENDPOINT)

    def job_endpoint(self) -> str | None:
        return self.get(*JOB_ENDPOINT)

    def platform_manifest(self) -> Any:
        return self.get_json(*PLATFORM_MANIFEST)

    def model_manifest(self) -> Any:
        return self.get_json(*MODEL_MANIFEST)

    def tool_limits_document(self) -> Any:
        return self.get_json(*TOOL_LIMITS)
