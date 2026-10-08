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
  the ``summary_top_n`` largest plus an ``other`` remainder;
* a ``model_selection`` run (FinanceModel ``payload.model_selection``, schema
  ``finplan.model_selection/1``: controls, traditional optimizers and RL compared on train /
  validation / test splits) adds a compact ``model_selection`` summary: the selection rule and
  frozen choice, the validation ranking, one metrics table per split, per RL algorithm the chosen
  configuration and seed, every seed's key metrics and the seed statistics (n, mean, std, min, max)
  per split, the promotion check, test-access record and caveats. Under size pressure the full
  ``payload.model_selection`` is dropped right after the candidate allocation (the summary and the
  run's evidence artifact keep it), and as a last step the per-seed rows (the statistics stay).

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
_CFG = re.compile(r"cfg_[0-9a-f]{64}")
_MAX_ROWS = 32
ROLES = ("optimizer", "control", "rl")
SPLITS = ("train", "validation", "test")
FAMILIES = ("control", "traditional", "rl")
SEED_METRICS = ("total_return", "sharpe", "max_drawdown", "ann_volatility", "turnover")
STAT_KEYS = ("n", "mean", "std", "min", "max")
_MAX_SEEDS = 32
_MAX_CAVEATS = 12
_MAX_TEXT = 600
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
        "Model-selection runs (job_type model_selection) add a model_selection summary: the "
        "validation-chosen model, per-split (train, validation, test) tables for controls, traditional "
        "optimizers and RL, RL per-seed metrics with mean/std/min/max across seeds, and the promotion "
        "check against the incumbent. Partial runs are flagged. Read-only."
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
        selection = model_selection_summary(doc["payload"].get("model_selection"))
        if selection is not None:
            doc["model_selection"] = selection
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
        if item.get("role") in ROLES:
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


# ------------------------------------------------------------------ model-selection summary
def _seed(v: Any) -> int | None:
    return v if isinstance(v, int) and not isinstance(v, bool) and 0 <= v < 10**9 else None


def _metrics(m: Any, keys: tuple[str, ...] = METRICS) -> dict[str, float | None]:
    return {k: _num(m[k]) for k in keys if k in m} if isinstance(m, Mapping) else {}


def _params(v: Any) -> dict[str, Any]:
    """Flat hyperparameters: reviewed names with finite numbers, booleans or short names only."""
    out: dict[str, Any] = {}
    if isinstance(v, Mapping):
        for k, raw in list(v.items())[:16]:
            if not _name(k):
                continue
            if isinstance(raw, bool) or _name(raw):
                out[k] = raw
            elif (n := _num(raw)) is not None:
                out[k] = n
    return out


def _cfg(v: Any) -> str | None:
    return v if isinstance(v, str) and _CFG.fullmatch(v) else None


def _date(v: Any) -> str | None:
    return v if isinstance(v, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", v) else None


def _split_rows(section: Any) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    strategies = section.get("strategies") if isinstance(section, Mapping) else None
    for item in (strategies if isinstance(strategies, list) else [])[:_MAX_ROWS]:
        if not isinstance(item, Mapping) or not (name := _name(item.get("strategy"))):
            continue
        row: dict[str, Any] = {"strategy": name}
        if item.get("family") in FAMILIES:
            row["family"] = item["family"]
        if item.get("role") in ROLES:
            row["role"] = item["role"]
        if (seed := _seed(item.get("seed"))) is not None:
            row["seed"] = seed
            if cid := _cfg(item.get("configuration_id")):
                row["configuration_id"] = cid
        if params := _params(item.get("params")):
            row["params"] = params
        for flag in ("selected", "incumbent"):
            if isinstance(item.get(flag), bool):
                row[flag] = item[flag]
        row.update(_metrics(item.get("metrics")))
        rows.append(row)
    return rows


def _stats(v: Any) -> dict[str, Any]:
    out: dict[str, Any] = {}
    if isinstance(v, Mapping):
        for metric in SEED_METRICS:
            st = v.get(metric)
            if isinstance(st, Mapping):
                out[metric] = {k: (_seed(st.get(k)) if k == "n" else _num(st.get(k))) for k in STAT_KEYS}
    return out


def _rl_block(block: Any) -> dict[str, Any] | None:
    if not isinstance(block, Mapping):
        return None
    out: dict[str, Any] = {}
    if _name(block.get("status")):
        out["status"] = block["status"]
    chosen = block.get("chosen")
    if isinstance(chosen, Mapping):
        out["chosen"] = {"seed": _seed(chosen.get("seed")), "reward": _params(chosen.get("reward")), "configuration_id": _cfg(chosen.get("configuration_id"))}
    hp = _params(block.get("hyperparameters"))
    if hp:
        out["hyperparameters"] = hp
    seeds = block.get("seeds")
    if isinstance(seeds, list):
        rows = []
        for s in seeds[:_MAX_SEEDS]:
            if not isinstance(s, Mapping) or (seed := _seed(s.get("seed"))) is None:
                continue
            row: dict[str, Any] = {"seed": seed}
            if isinstance(s.get("selected"), bool):
                row["selected"] = s["selected"]
            for split in SPLITS:
                if isinstance(s.get(split), Mapping):
                    row[split] = _metrics(s[split], SEED_METRICS)
            rows.append(row)
        out["seeds"] = rows
    stats = block.get("seed_statistics")
    if isinstance(stats, Mapping):
        out["seed_statistics"] = {split: _stats(stats.get(split)) for split in SPLITS if isinstance(stats.get(split), Mapping)}
    return out or None


def model_selection_summary(ms: Any) -> dict[str, Any] | None:
    """Compact view of FinanceModel's ``payload.model_selection`` (controls vs traditional vs RL).

    Like :func:`comparison_summary`, only reviewed fields are copied (names matched, numbers finite,
    free text cut), so an unexpected producer shape yields a smaller summary, never an unreviewed value.
    """
    if not isinstance(ms, Mapping) or not isinstance(ms.get("selection"), Mapping):
        return None
    out: dict[str, Any] = {}
    if isinstance(ms.get("schema"), str) and re.fullmatch(r"finplan\.model_selection/\d+", ms["schema"]):
        out["schema"] = ms["schema"]
    data = ms.get("data")
    if isinstance(data, Mapping):
        splits = data.get("splits") if isinstance(data.get("splits"), Mapping) else {}
        out["splits"] = {
            s: {"start": _date(splits[s].get("start")), "end": _date(splits[s].get("end")), "sessions": _seed(splits[s].get("sessions"))}
            for s in SPLITS
            if isinstance(splits.get(s), Mapping)
        }
        if isinstance(data.get("universe"), list):
            out["universe"] = [u for u in data["universe"][:_MAX_WEIGHTS] if _name(u)]
    ev = ms.get("evaluation")
    if isinstance(ev, Mapping):
        out["evaluation"] = {k: ev[k] for k in ("rebalance_frequency", "execution_timing") if _name(ev.get(k))}
        if isinstance(ev.get("long_only"), bool):
            out["evaluation"]["long_only"] = ev["long_only"]
        if (mw := _num(ev.get("max_weight"))) is not None:
            out["evaluation"]["max_weight"] = mw
        out["evaluation"]["same_evaluator_for_all_families"] = True
    sel = ms["selection"]
    rule = sel.get("rule") if isinstance(sel.get("rule"), Mapping) else {}
    metric = _name(rule.get("metric"))
    chosen = sel.get("selected") if isinstance(sel.get("selected"), Mapping) else {}
    out["selection"] = {
        "rule": {"metric": metric, "split": _name(rule.get("split")), "direction": "higher is better"},
        "selected": {"strategy": _name(chosen.get("strategy")), "family": chosen.get("family") if chosen.get("family") in FAMILIES else None, "seed": _seed(chosen.get("seed"))},
        "incumbent": _name(sel.get("incumbent")),
    }
    if isinstance(sel.get("selection_checksum"), str) and re.fullmatch(r"sha256:[0-9a-f]{64}", sel["selection_checksum"]):
        out["selection"]["selection_checksum"] = sel["selection_checksum"]
    ranking = sel.get("validation_ranking")
    if isinstance(ranking, list) and metric:
        key = f"validation_{metric}"
        out["selection"]["validation_ranking"] = [
            {"strategy": r["strategy"], "family": r.get("family") if r.get("family") in FAMILIES else None, "seed": _seed(r.get("seed")), key: _num(r.get(key))}
            for r in ranking[:_MAX_ROWS]
            if isinstance(r, Mapping) and _name(r.get("strategy"))
        ]
    comparison = ms.get("comparison")
    if isinstance(comparison, Mapping):
        out["columns"] = ["strategy", "family", "role", "seed", *METRICS]
        out["by_split"] = {s: _split_rows(comparison.get(s)) for s in SPLITS if isinstance(comparison.get(s), Mapping)}
    rl = ms.get("rl")
    if isinstance(rl, Mapping):
        out["rl"] = {algo: b for algo, raw in list(rl.items())[:8] if _name(algo) and (b := _rl_block(raw)) is not None}
    gate = ms.get("promotion_check")
    if isinstance(gate, Mapping):
        g: dict[str, Any] = {k: gate[k] for k in ("candidate", "incumbent", "incumbent_source", "split", "result", "reason") if _name(gate.get(k))}
        g["candidate_seed"] = _seed(gate.get("candidate_seed"))
        for k in ("requires_user_approval", "r1_net_return_beats_incumbent", "r2_drawdown_not_worse"):
            if isinstance(gate.get(k), bool):
                g[k] = gate[k]
        for k in ("candidate_test", "incumbent_test"):
            if isinstance(gate.get(k), Mapping):
                g[k] = _metrics(gate[k], ("total_return", "max_drawdown"))
        out["promotion_check"] = g
    ta = ms.get("test_access")
    if isinstance(ta, Mapping):
        out["test_access"] = {k: ta[k] for k in ("evaluated_once_in_this_run", "test_reuse") if isinstance(ta.get(k), bool)}
        if (n := _seed(ta.get("prior_runs_on_this_test_period"))) is not None:
            out["test_access"]["prior_runs_on_this_test_period"] = n
    cav = ms.get("caveats")
    if isinstance(cav, list):
        out["caveats"] = [{"kind": c["kind"], "text": c["text"][:_MAX_TEXT]} for c in cav[:_MAX_CAVEATS] if isinstance(c, Mapping) and _name(c.get("kind")) and isinstance(c.get("text"), str)]
    if isinstance(ms.get("result_trimmed"), bool):
        out["result_trimmed"] = ms["result_trimmed"]
    return out


def _top_n(weights: Mapping[str, float], n: int) -> dict[str, float]:
    items = sorted(weights.items(), key=lambda kv: (-abs(kv[1]), kv[0]))
    out = dict(items[:n])
    if len(items) > n:
        out["other"] = sum(w for _, w in items[n:])
    return out


def _fit(doc: dict[str, Any], limits: Any) -> None:
    """Drop, in order, the candidate allocation, the full model-selection block, the full benchmark
    block, then trim the weights and finally the per-seed RL rows."""
    notes: list[str] = []
    payload = dict(doc["payload"])
    if response_size(doc) > limits.response_max_bytes and payload.pop("proposed_allocation", None) is not None:
        doc["payload"] = payload
        notes.append("The candidate allocation is omitted to fit the response size; read it through the artifact references.")
    if response_size(doc) > limits.response_max_bytes and payload.pop("model_selection", None) is not None:
        doc["payload"] = payload
        notes.append("The full model-selection block is omitted to fit the response size; the model_selection summary is kept and the run artifact holds every detail.")
    if response_size(doc) > limits.response_max_bytes and payload.pop("benchmark", None) is not None:
        doc["payload"] = payload
        notes.append("The full per-strategy benchmark block is omitted to fit the response size; the comparison table is kept.")
    pw = (doc.get("comparison") or {}).get("primary_weights")
    if response_size(doc) > limits.response_max_bytes and isinstance(pw, dict):
        n = max(1, int(getattr(limits, "summary_top_n", 10)))
        pw["final"], pw["average"] = _top_n(pw["final"], n), _top_n(pw["average"], n)
        pw["top_n"] = n
        notes.append(f"The optimizer weights are cut to the {n} largest plus 'other'.")
    rl = (doc.get("model_selection") or {}).get("rl")
    if response_size(doc) > limits.response_max_bytes and isinstance(rl, dict):
        for block in rl.values():
            block.pop("seeds", None)
        notes.append("The per-seed RL rows are omitted to fit the response size; the seed statistics are kept.")
    if notes:
        doc["truncated"] = True
        doc["message"] = " ".join(notes)
