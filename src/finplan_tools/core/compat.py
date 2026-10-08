"""Snapshot and configuration compatibility checks (D5 steps 1, 3, 4; EXP-03, EXP-08).

* :func:`local_configuration_id` computes the content-addressed ``configuration_id`` with the
  contract package (``cfg_`` + SHA-256 of the RFC 8785 canonical configuration).
* :func:`check_configuration_id` compares it with the producer's value (mismatch -> ``INTERNAL``:
  the two sides disagree on the canonical form, which is a defect, never a caller error).
* :func:`check_snapshot_compatibility` checks a platform snapshot read in this environment against
  the request: same domain, ``approved`` status (optional), coverage spanning the window.
* :func:`check_producer_major` checks a producer release manifest serves the pinned major.
"""

from __future__ import annotations

from typing import Any, Mapping

from finplan_contracts.canonical import configuration_id as _configuration_id

from .contracts import contract_major
from .errors import DEPENDENCY_UNAVAILABLE, PRECONDITION_FAILED, UNSUPPORTED_CONTRACT_VERSION, ToolError

__all__ = ["local_configuration_id", "check_configuration_id", "check_snapshot_compatibility", "check_producer_major", "check_producer_release", "producer_availability"]


def local_configuration_id(configuration: Any) -> str:
    return _configuration_id(configuration)


def check_configuration_id(local: str, producer: Any) -> None:
    if producer != local:
        raise ToolError.internal("the producer configuration_id differs from the locally computed one", reason="configuration_id_mismatch")


def check_snapshot_compatibility(
    snapshot: Mapping[str, Any],
    *,
    domain: str | None = None,
    window: Mapping[str, str] | None = None,
    require_approved: bool = False,
) -> None:
    """Raise ``PRECONDITION_FAILED`` when the snapshot cannot serve the request."""
    sid = snapshot.get("input_snapshot_id")
    if domain is not None and snapshot.get("domain") != domain:
        raise ToolError(PRECONDITION_FAILED, "the snapshot belongs to another domain", reason="domain_mismatch", input_snapshot_id=sid)
    status = snapshot.get("status")
    if require_approved and status != "approved":
        raise ToolError(PRECONDITION_FAILED, "the snapshot is not approved", reason="snapshot_not_approved", status=status, input_snapshot_id=sid)
    if window is not None:
        cov = snapshot.get("coverage") or {}
        start, end = cov.get("start"), cov.get("end")
        if not (isinstance(start, str) and isinstance(end, str) and start <= window.get("start", "") and window.get("end", "") <= end):
            raise ToolError(
                PRECONDITION_FAILED,
                "the snapshot coverage does not span the requested window",
                reason="coverage_gap",
                coverage={"start": start, "end": end},
                requested={"start": window.get("start"), "end": window.get("end")},
                input_snapshot_id=sid,
            )


def check_producer_major(manifest: Mapping[str, Any] | None, producer: str) -> None:
    """Raise when a producer release manifest is absent or does not serve the pinned major."""
    # Not retryable (spec experiment-tools "Model-backed tools gated on producer availability"): the
    # producer is not released here, which a retry of the same call cannot change.
    if not manifest:
        raise ToolError(DEPENDENCY_UNAVAILABLE, f"no {producer} release is published in this environment", retryable=False, producer=producer, reason="producer_not_released")
    served = sorted({m for m in (manifest.get("served_contract_majors") or []) if isinstance(m, int) and not isinstance(m, bool) and m >= 0})
    if not served:
        raise ToolError(DEPENDENCY_UNAVAILABLE, f"the {producer} release manifest lists no served contract majors", retryable=False, producer=producer, reason="producer_not_released")
    if contract_major() not in served:
        raise ToolError(UNSUPPORTED_CONTRACT_VERSION, f"the {producer} release does not serve contract major {contract_major()}", details={"served_contract_majors": served, "producer": producer})


def _semver(value: Any) -> tuple[int, int, int] | None:
    if not isinstance(value, str):
        return None
    parts = value.split("+", 1)[0].split("-", 1)[0].split(".")
    if len(parts) != 3 or not all(p.isdigit() for p in parts):
        return None
    return int(parts[0]), int(parts[1]), int(parts[2])


def check_producer_release(manifest: Mapping[str, Any] | None, producer: str, minimum: str | None = None) -> None:
    """:func:`check_producer_major`, then (when ``minimum`` is set) require the producer release to
    declare ``contract_version`` >= ``minimum``: an older release does not serve the operation yet,
    which is ``DEPENDENCY_UNAVAILABLE`` (not retryable) and never a call to a missing route."""
    check_producer_major(manifest, producer)
    if minimum is None:
        return
    declared = _semver((manifest or {}).get("contract_version"))
    need = _semver(minimum)
    if need is None:  # pragma: no cover - catalog constant
        raise ValueError(f"bad minimum contract version {minimum!r}")
    if declared is None or declared < need:
        raise ToolError(
            DEPENDENCY_UNAVAILABLE,
            f"the {producer} release in this environment does not serve this operation yet (needs contract {minimum})",
            retryable=False,
            producer=producer,
            reason="operation_not_released",
            required_contract_version=minimum,
        )


def producer_availability(manifest: Mapping[str, Any] | None, minimum: str | None = None) -> tuple[bool, str | None]:
    """(available, reason code) for describe_capabilities: absent -> DEPENDENCY_UNAVAILABLE."""
    try:
        check_producer_release(manifest, "producer", minimum)
    except ToolError as exc:
        return False, exc.code
    return True, None
