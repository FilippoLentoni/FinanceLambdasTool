"""``get_experiment_result`` (spec experiment-tools; task 6.5).

FinanceModel ``GET /v1/jobs/{run_id}/result`` (contract ``job-result``):

* ``completion_status`` and ``solution_status`` stay separate: infeasible, unbounded or no-effect
  outcomes are ``succeeded`` runs and a successful tool call; a crash is ``failed`` with
  FinanceModel's error envelope in ``error`` and no ``solution_status``;
* a non-terminal run -> FinanceModel's ``PRECONDITION_FAILED`` with ``details.state`` passed through;
* ``timed_out`` / ``cancelled`` runs or incomplete outputs: ``artifacts_complete`` false, only the
  references FinanceModel lists, and no summary metrics (the payload's metric sections would describe
  missing periods), with a note that the result is not a usable plan input;
* a succeeded result keeps FinanceModel's separate sections (portfolio ``performance``, model
  ``accuracy``, ``compute_cost``), the lineage identifiers and the artifact references with checksums;
  when it would exceed the byte limit the candidate allocation is left out (``truncated``) and stays
  reachable through the artifact references;
* when FinanceModel's payload carries the strategy comparison (``payload.benchmark``: every strategy
  of a ``run_benchmark``/``run_backtest`` run with metrics, weights and units), the response adds a
  compact ``comparison``: a table (one row per strategy: role and the key metrics), the primary
  (optimizer) strategy's final and average weights, the evaluation window, the risk-free assumption
  and the unit legend. Under size pressure the drop order is: candidate allocation, the full
  ``payload.benchmark`` (the ``comparison`` keeps the table), then the primary weights are cut to
  the ``summary_top_n`` largest plus an ``other`` remainder.

Read-only, FinanceModel only: no platform call, and a result never becomes a plan version through a
tool (promotion stays the platform's staged-output acceptance path).
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping
from typing import Any

from ..core.bounds import response_size
from ..core.errors import ToolError
from ..core.registry import register_tool
from ..core.status import outcome
from ._common import producer_doc, project

RESPONSE = "tools/get-experiment-result-response"
METRICS = ("total_return", "cagr", "ann_volatility", "sharpe", "max_drawdown", "turnover", "transaction_cost", "transaction_cost_fraction")
_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$")
_MAX_ROWS = 32
_MAX_WEIGHTS = 64
PARTIAL_MESSAGE = "The run did not complete with all outputs; only the listed references exist and the result is not a usable plan input."


@register_tool(
    "get_experiment_result",
    description=(
        "Read the result of a finished FinanceModel run by run_id: completion_status (did the run "
        "finish) separately from solution_status (optimal, feasible, infeasible, unbounded, no_effect), "
        "separate portfolio-performance, model-accuracy and compute-cost sections, lineage identifiers "
        "and trusted artifact references. Benchmark and backtest runs add a comparison table (each "
        "strategy and control with total_return, cagr, ann_volatility, sharpe, max_drawdown, "
        "turnover, transaction_cost; units included) and the optimizer's final and average weights. "
        "Partial runs are flagged. Read-only."
    ),
)
def get_experiment_result(ctx: Any, request: dict[str, Any]) -> dict[str, Any]:
    result = producer_doc(ctx.jobs.get_job_result(request["run_id"], ctx.meta), "job result")
    if result.get("run_id") != request["run_id"]:
        raise ToolError.internal("FinanceModel returned the result of another run")
    doc = project(result, RESPONSE)
    view = outcome(result)
    if view["completion_status"] != "succeeded":
        doc.pop("solution_status", None)
    if view["partial"]:
        doc["artifacts_complete"] = False
        doc.pop("payload", None)
        doc["message"] = PARTIAL_MESSAGE
    elif isinstance(doc.get("payload"), dict):
        comparison = comparison_summary(doc["payload"].get("benchmark"))
        if comparison is not None:
            doc["comparison"] = comparison
        _fit(doc, ctx.limits)
    if request.get("synthetic") is True:
        doc["synthetic"] = True
    return doc


# ------------------------------------------------------------------ comparison summary
def _num(v: Any) -> float | None:
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(float(v)):
        return None
    return float(v)


def _name(v: Any) -> str | None:
    return v if isinstance(v, str) and _NAME.match(v) else None


def _weights(v: Any) -> dict[str, float]:
    if not isinstance(v, Mapping):
        return {}
    out = {k: w for k, raw in v.items() if _name(k) and (w := _num(raw)) is not None}
    return dict(sorted(out.items(), key=lambda kv: (-abs(kv[1]), kv[0]))[:_MAX_WEIGHTS])


def comparison_summary(bench: Any) -> dict[str, Any] | None:
    """Compact strategy -> metrics table plus the primary strategy's weights, from ``payload.benchmark``.

    Only reviewed fields are copied (names matched, numbers finite), so an unexpected producer shape
    yields a smaller summary, never an unreviewed value.
    """
    if not isinstance(bench, Mapping) or not isinstance(bench.get("strategies"), list):
        return None
    primary = _name(bench.get("primary_strategy"))
    rows: list[dict[str, Any]] = []
    weights: dict[str, Any] | None = None
    for item in bench["strategies"][:_MAX_ROWS]:
        if not isinstance(item, Mapping) or not (name := _name(item.get("strategy"))):
            continue
        metrics = item.get("metrics") if isinstance(item.get("metrics"), Mapping) else {}
        row: dict[str, Any] = {"strategy": name}
        if item.get("role") in ("optimizer", "control"):
            row["role"] = item["role"]
        if _name(item.get("solution_status")):
            row["solution_status"] = item["solution_status"]
        for k in METRICS:
            if k in metrics:
                row[k] = _num(metrics[k])  # null stays null (for example an undefined Sharpe ratio)
        rows.append(row)
        if name == primary and weights is None:
            weights = {
                "strategy": name,
                "final": _weights(item.get("final_weights")),
                "final_cash": _num(item.get("final_cash_weight")),
                "average": _weights(item.get("average_weights")),
                "average_cash": _num(item.get("average_cash_weight")),
            }
    if not rows:
        return None
    out: dict[str, Any] = {"primary_strategy": primary, "columns": ["strategy", "role", *METRICS], "rows": rows}
    win = bench.get("evaluation_window")
    if isinstance(win, Mapping):
        out["evaluation_window"] = {k: win[k] for k in ("start", "end", "sessions") if isinstance(win.get(k), (str, int)) and not isinstance(win.get(k), bool)}
    rf = bench.get("risk_free")
    if isinstance(rf, Mapping):
        out["risk_free"] = {"annual_rate": _num(rf.get("annual_rate")), "source": _name(rf.get("source"))}
    if _name(bench.get("base_currency")):
        out["base_currency"] = bench["base_currency"]
    if (cap := _num(bench.get("initial_capital"))) is not None:
        out["initial_capital"] = cap
    units = bench.get("units")
    if isinstance(units, Mapping):
        out["units"] = {k: str(units[k])[:200] for k in (*METRICS, "weights") if isinstance(units.get(k), str)}
    if weights is not None:
        out["primary_weights"] = weights
    return out


def _top_n(weights: Mapping[str, float], n: int) -> dict[str, float]:
    items = sorted(weights.items(), key=lambda kv: (-abs(kv[1]), kv[0]))
    out = dict(items[:n])
    if len(items) > n:
        out["other"] = sum(w for _, w in items[n:])
    return out


def _fit(doc: dict[str, Any], limits: Any) -> None:
    """Drop, in order, the candidate allocation, the full benchmark block, then trim the weights."""
    notes: list[str] = []
    payload = dict(doc["payload"])
    if response_size(doc) > limits.response_max_bytes and payload.pop("proposed_allocation", None) is not None:
        doc["payload"] = payload
        notes.append("The candidate allocation is omitted to fit the response size; read it through the artifact references.")
    if response_size(doc) > limits.response_max_bytes and payload.pop("benchmark", None) is not None:
        doc["payload"] = payload
        notes.append("The full per-strategy benchmark block is omitted to fit the response size; the comparison table is kept.")
    pw = (doc.get("comparison") or {}).get("primary_weights")
    if response_size(doc) > limits.response_max_bytes and isinstance(pw, dict):
        n = max(1, int(getattr(limits, "summary_top_n", 10)))
        pw["final"], pw["average"] = _top_n(pw["final"], n), _top_n(pw["average"], n)
        pw["top_n"] = n
        notes.append(f"The optimizer weights are cut to the {n} largest plus 'other'.")
    if notes:
        doc["truncated"] = True
        doc["message"] = " ".join(notes)
