"""Completion status versus solution status (contracts job-status/job-result; proposal "What Changes").

``completion_status`` says whether the run finished (``succeeded``, ``failed``, ``cancelled``,
``timed_out``); ``solution_status`` says what the finished computation found (``optimal``,
``feasible``, ``infeasible``, ``unbounded``, ``no_effect``, ``not_applicable``). An infeasible or
no-effect outcome is a *successful* run, never a failure. Partial outputs are explicit.
"""

from __future__ import annotations

from typing import Any, Mapping

from .contracts import store

__all__ = ["COMPLETION_STATUSES", "SOLUTION_STATUSES", "TERMINAL_STATES", "WAITING_STATES", "is_terminal", "outcome", "is_failure"]


def _enum(name: str) -> tuple[str, ...]:
    return tuple(store().get("job-status").schema["$defs"][name]["enum"])


COMPLETION_STATUSES = _enum("completion_status")
SOLUTION_STATUSES = _enum("solution_status")
TERMINAL_STATES = COMPLETION_STATUSES
WAITING_STATES = ("awaiting_approval", "queued")


def is_terminal(state: Any) -> bool:
    return state in TERMINAL_STATES


def is_failure(doc: Mapping[str, Any]) -> bool:
    """True only for runs that did not complete; infeasible/no_effect solutions are not failures."""
    return doc.get("completion_status") in ("failed", "cancelled", "timed_out")


def outcome(doc: Mapping[str, Any]) -> dict[str, Any]:
    """Separate completion/solution view of a job-status or job-result document.

    ``partial`` is true when the run did not succeed or its artifacts are incomplete.
    """
    completion = doc.get("completion_status")
    if completion is None and is_terminal(doc.get("state")):
        completion = doc.get("state")
    solution = doc.get("solution_status") if completion == "succeeded" else None
    artifacts_complete = doc.get("artifacts_complete")
    out: dict[str, Any] = {
        "completion_status": completion,
        "solution_status": solution,
        "failed": completion in ("failed", "cancelled", "timed_out"),
        "partial": bool(completion is not None and (completion != "succeeded" or artifacts_complete is False)),
    }
    if artifacts_complete is not None:
        out["artifacts_complete"] = bool(artifacts_complete)
    return out
