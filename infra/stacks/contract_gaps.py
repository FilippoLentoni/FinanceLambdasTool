"""Switches for IaC that waits on a contract change (reported as CONTRACT GAP; never enabled by default).

The pinned ownership matrix decides what a template may declare (OWN-01). Where this repository
needs a resource type its matrix row does not list yet, the construct exists behind a gap switch:
off by default (so the ownership gate passes with the pinned contracts), switched on with
``-c finplan:contract-gaps=<gap>[,<gap>]`` once the contract release that adds the row is pinned.

===============  ===========================================================================
gap              what it enables and the interim
===============  ===========================================================================
pipeline-logs    explicit 30-day ``AWS::Logs::LogGroup`` per CodeBuild project, tagged
                 ``logical-role=pipeline-build-project`` (needs ``AWS::Logs::LogGroup`` in the
                 matrix row ``pipeline-financelambdastool``, as contracts 0.2.1 / 1.0.0 added for
                 FinancialPlanning and FinanceModel). Interim (lesson L6): the bootstrap creates
                 ``/aws/codebuild/<project>`` with the cost tags and sets 30-day retention before
                 the first build can run (:func:`scripts.bootstrap.apply_codebuild_log_retention`).
===============  ===========================================================================
"""

from __future__ import annotations

from typing import Any

__all__ = ["CONTEXT_KEY", "KNOWN_GAPS", "enabled_gaps"]

CONTEXT_KEY = "finplan:contract-gaps"
KNOWN_GAPS = {"pipeline-logs": "AWS::Logs::LogGroup in matrix row pipeline-financelambdastool"}


def enabled_gaps(raw: Any = None) -> frozenset[str]:
    if not raw:
        return frozenset()
    items = raw if isinstance(raw, (list, tuple)) else str(raw).split(",")
    gaps = {str(i).strip() for i in items if str(i).strip()}
    unknown = sorted(gaps - set(KNOWN_GAPS))
    if unknown:
        raise ValueError(f"unknown contract gaps {unknown}; known: {sorted(KNOWN_GAPS)}")
    return frozenset(gaps)
