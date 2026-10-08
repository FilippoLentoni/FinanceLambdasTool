"""In-process FinancialPlanning plan/ingestion API double (TEST-ONLY).

Routes and response schemas match the platform route table (``finplan_platform.handlers.api``):
every answer is validated against the same contract schema the platform validates against, so the
mock passes producer-mode conformance (CS-10). Semantics modelled:

* plan head and ``revision``; ``expected_revision`` mismatch -> ``CONFLICT``;
* override versions are children of an existing version of the same plan; the parent never changes;
  identical content -> child with ``no_effect`` true;
* validation (weights plus cash must sum to 1) -> ``validated`` or ``invalid`` with the finding;
* publication only of a ``validated`` version (else ``PRECONDITION_FAILED``), superseding the
  current publication;
* phase 1 synthetic-only writes: a non-synthetic portfolio -> ``OPERATION_NOT_PERMITTED``;
* idempotency per operation and key (replay or ``IDEMPOTENCY_KEY_REUSED``);
* ingestion of the ETF daily dataset into a new ``committed`` snapshot, ``daily`` only;
* snapshot and observation reads (``partial`` with uncovered ranges when the range is not covered).

Fault injection: ``fail_next(op, code)``, ``unreachable``, ``missing_routes`` (older release).
"""

from __future__ import annotations

import copy
import hashlib
from datetime import date, timedelta
from typing import Any, Mapping

from finplan_contracts.canonical import canonicalize

from ._base import MockProducer, fixture, require_valid

#: Query parameters of ``GET /v1/snapshots/{id}/observations`` as the platform route declares them
#: (``instrument_id`` is a repeated/comma-separated list parameter).
OBSERVATION_QUERY = frozenset({"instrument_id", "start_date", "end_date", "page_size", "next_token"})

PLAN_ROUTES = (
    "get_plan",
    "list_plan_versions",
    "get_plan_version",
    "get_portfolio",
    "get_snapshot",
    "read_snapshot_observations",
    "create_plan_version",
    "validate_plan_version",
    "publish_plan_version",
    "run_ingestion",
)


def _checksum(obj: Any) -> str:
    return "sha256:" + hashlib.sha256(canonicalize(obj)).hexdigest()


def _sessions(start: str, end: str) -> list[str]:
    d, last, out = date.fromisoformat(start), date.fromisoformat(end), []
    while d <= last:
        if d.weekday() < 5:
            out.append(d.isoformat())
        d += timedelta(days=1)
    return out


class MockPlatform(MockProducer):
    producer = "financialplanning"

    def __init__(self, **kw: Any) -> None:
        super().__init__(**kw)
        self.portfolios: dict[str, dict[str, Any]] = {}
        self.plans: dict[str, dict[str, Any]] = {}
        self.versions: dict[str, dict[str, Any]] = {}
        self.contents: dict[str, dict[str, Any]] = {}
        self.publications: dict[str, dict[str, Any]] = {}
        self.snapshots: dict[str, dict[str, Any]] = {}
        self.observations: dict[str, list[dict[str, Any]]] = {}
        self.declared_granularities = ["daily"]
        self.ingestion_quality_flags: list[str] = []
        self.ingestion_coverage_end: str | None = None  # simulate a partial provider response
        self.observation_queries: list[dict[str, Any]] = []
        r = self.route
        r("GET", "v1/plans/{plan_id}", "get_plan", self._get_plan)
        r("GET", "v1/plans/{plan_id}/versions", "list_plan_versions", self._list_versions)
        r("POST", "v1/plans/{plan_id}/versions", "create_plan_version", self._create_version)
        r("GET", "v1/plan-versions/{plan_version_id}", "get_plan_version", self._get_version)
        r("POST", "v1/plan-versions/{plan_version_id}/validate", "validate_plan_version", self._validate)
        r("POST", "v1/plans/{plan_id}/publications", "publish_plan_version", self._publish)
        r("GET", "v1/portfolios/{portfolio_id}", "get_portfolio", self._get_portfolio)
        r("GET", "v1/snapshots/{input_snapshot_id}", "get_snapshot", self._get_snapshot)
        r("GET", "v1/snapshots/{input_snapshot_id}/observations", "read_snapshot_observations", self._read_observations)
        # the ingestion reference is the full POST URL: the transport sends an empty path
        r("POST", "", "run_ingestion", self._ingest)
        r("POST", "v1/ingestions", "run_ingestion", self._ingest)

    # ================================================================ seeding
    def add_portfolio(self, *, synthetic: bool = True, name: str = "Synthetic portfolio") -> str:
        doc = fixture("portfolio", "synthetic-portfolio")
        pid = self.ids.mint("pf")
        doc.update(portfolio_id=pid, name=name, synthetic=synthetic, created_at=self.clock.iso())
        if not synthetic:
            doc.pop("synthetic", None)
            doc["synthetic"] = False
        self.portfolios[pid] = require_valid(doc, "portfolio")
        return pid

    def add_plan(self, portfolio_id: str, *, content: Mapping[str, Any] | None = None, status: str = "validated", synthetic: bool = True) -> tuple[str, str]:
        """A plan with one root version (``origin`` model_run); returns (plan_id, plan_version_id)."""
        plan_id = self.ids.mint("pl")
        pv = self._new_version(plan_id, None, dict(content or self.default_content()), origin="model_run", status=status, synthetic=synthetic)
        self.plans[plan_id] = {
            "plan_id": plan_id,
            "portfolio_id": portfolio_id,
            "name": "Synthetic plan",
            "head": {"current_version_id": pv, "revision": 1},
            "current_publication_id": None,
            "created_at": self.clock.iso(),
            "synthetic": synthetic,
        }
        return plan_id, pv

    @staticmethod
    def default_content() -> dict[str, Any]:
        return copy.deepcopy(fixture("tools/create-override-version-request", "override")["content"])

    def add_snapshot(self, *, status: str = "approved", start: str = "2026-01-02", end: str = "2026-01-09", quality_flags: list[str] | None = None, lineage: Mapping[str, Any] | None = None, observed_end: str | None = None, quality_details: Mapping[str, Any] | None = None) -> str:
        """An S&P 500 tracking-ETF daily snapshot (``finance/etf-daily/SPY``) with synthetic observations."""
        sid = self.ids.mint("snap")
        doc = fixture("input-snapshot", "approved-etf-daily")
        doc.update(input_snapshot_id=sid, status=status, coverage={"start": start, "end": end}, quality_flags=list(quality_flags or []), created_at=self.clock.iso())
        if lineage is not None:
            doc["lineage"] = dict(lineage)
        if quality_details is not None:
            doc["quality_details"] = dict(quality_details)
        if status != "approved":
            doc.pop("approval_rule_version", None)
        self.snapshots[sid] = require_valid(doc, "input-snapshot")
        sessions = _sessions(start, observed_end or end)
        self.observations[sid] = [
            {"instrument_id": "SPY", "session_date": d, "kind": "completed_daily", "session_status": "regular", "open": 100.0 + i, "high": 101.5 + i, "low": 99.5 + i, "close": 101.0 + i, "volume": 1000, "synthetic": True}
            for i, d in enumerate(sessions)
        ]
        return sid

    def _new_version(self, plan_id: str, parent: str | None, content: dict[str, Any], *, origin: str, status: str, synthetic: bool = True) -> str:
        pv = self.ids.mint("pv")
        base = fixture("tools/get-plan-version-response", "compact")["plan_version"]
        base.update(
            plan_version_id=pv,
            plan_id=plan_id,
            parent_plan_version_id=parent,
            origin=origin,
            status=status,
            checksum=_checksum(content),
            created_at=self.clock.iso(),
            synthetic=synthetic,
        )
        base["content_ref"] = dict(base["content_ref"], artifact_id=self.ids.mint("art"), checksum=_checksum({"content": content}))
        if origin != "model_run":
            # a child keeps its parent's lineage; only a model run has a run_id
            for k in ("model_version", "configuration_id", "input_snapshot_id"):
                if parent is not None:
                    base[k] = self.versions[parent][k]
            base["run_id"] = None
        self.versions[pv] = require_valid(base, "plan-version")
        self.contents[pv] = content
        return pv

    # ================================================================ reads
    def _plan(self, plan_id: str) -> dict[str, Any] | None:
        return self.plans.get(plan_id)

    def _get_plan(self, request: Any, plan_id: str) -> tuple[int, Any]:
        plan = self._plan(plan_id)
        if plan is None:
            return self.error("NOT_FOUND", "plan not found", record_type="plan")
        pub = self.publications.get(plan["current_publication_id"]) if plan["current_publication_id"] else None
        body = {"plan": copy.deepcopy(plan), "current_publication": copy.deepcopy(pub), "synthetic": True}
        return 200, require_valid(body, "tools/get-plan-response")

    def _summary(self, pv: str) -> dict[str, Any]:
        v = self.versions[pv]
        return {k: v[k] for k in ("plan_version_id", "parent_plan_version_id", "origin", "status", "checksum", "created_at")}

    def _list_versions(self, request: Any, plan_id: str) -> tuple[int, Any]:
        plan = self._plan(plan_id)
        if plan is None:
            return self.error("NOT_FOUND", "plan not found", record_type="plan")
        q = {k: v for k, v in (request.query or {}).items() if v is not None}
        unknown = sorted(set(q) - {"page_size", "next_token"})
        if unknown:
            return self.error("VALIDATION_FAILED", f"unknown query parameter {unknown[0]!r}", pointer=f"/{unknown[0]}")
        size = int(q.get("page_size") or 20)
        start = int(q.get("next_token")[1:]) if isinstance(q.get("next_token"), str) and q["next_token"].startswith("p") else 0
        ids = sorted((pv for pv, v in self.versions.items() if v["plan_id"] == plan_id), key=lambda pv: self.versions[pv]["created_at"] + pv, reverse=True)
        page = ids[start : start + size]
        token = f"p{start + size}" if start + size < len(ids) else None
        body = {"plan_id": plan_id, "versions": [self._summary(pv) for pv in page], "next_token": token, "synthetic": True}
        return 200, require_valid(body, "tools/list-plan-versions-response")

    def _get_version(self, request: Any, plan_version_id: str) -> tuple[int, Any]:
        v = self.versions.get(plan_version_id)
        if v is None:
            return self.error("NOT_FOUND", "plan version not found", record_type="plan_version")
        content = self.contents[plan_version_id]
        weights = content.get("allocation", {}).get("weights", [])
        body = {
            "plan_version": copy.deepcopy(v),
            "content_summary": {"top_weights": copy.deepcopy(weights[:10]), "other_weight": round(1 - sum(w["weight"] for w in weights[:10]), 12)},
            "content_ref": copy.deepcopy(v["content_ref"]),
            "truncated": False,
            "synthetic": True,
        }
        return 200, require_valid(body, "tools/get-plan-version-response")

    def _get_portfolio(self, request: Any, portfolio_id: str) -> tuple[int, Any]:
        p = self.portfolios.get(portfolio_id)
        return (200, copy.deepcopy(p)) if p else self.error("NOT_FOUND", "portfolio not found", record_type="portfolio")

    def _get_snapshot(self, request: Any, input_snapshot_id: str) -> tuple[int, Any]:
        s = self.snapshots.get(input_snapshot_id)
        if s is None:
            return self.error("NOT_FOUND", "snapshot not found", record_type="input_snapshot")
        return 200, {"snapshot": copy.deepcopy(s), "synthetic": True}

    def _read_observations(self, request: Any, input_snapshot_id: str) -> tuple[int, Any]:
        s = self.snapshots.get(input_snapshot_id)
        if s is None:
            return self.error("NOT_FOUND", "snapshot not found", record_type="input_snapshot")
        q = {k: v for k, v in (request.query or {}).items() if v is not None}
        unknown = sorted(set(q) - OBSERVATION_QUERY)
        if unknown:  # the platform route declares its query parameters and refuses others
            return self.error("VALIDATION_FAILED", f"unknown query parameter {unknown[0]!r}", pointer=f"/{unknown[0]}")
        self.observation_queries.append(dict(q))
        wanted = q.get("instrument_id")
        wanted = set([wanted] if isinstance(wanted, str) else wanted or [])
        start = q.get("start_date") or s["coverage"]["start"]
        end = q.get("end_date") or s["coverage"]["end"]
        obs = [o for o in self.observations.get(input_snapshot_id, []) if start <= o["session_date"] <= end and (not wanted or o["instrument_id"] in wanted)]
        have = {o["session_date"] for o in obs}
        missing = [d for d in _sessions(start, end) if d not in have]
        uncovered = []
        if missing:
            uncovered.append({"start": missing[0], "end": missing[-1]})
        closes = [o["close"] for o in obs]
        instruments = [{"instrument_id": "SPY", "observation_count": len(obs), "first_date": obs[0]["session_date"], "last_date": obs[-1]["session_date"], "close_min": min(closes), "close_max": max(closes), "close_last": closes[-1]}] if obs else []
        body = {
            "snapshot": copy.deepcopy(s),
            "observation_kinds": ["completed_daily"],
            "instruments": instruments,
            "partial": bool(missing),
            "data_refs": [a for a in copy.deepcopy(s.get("artifacts", [])) if a.get("kind") == "snapshot_payload"],
            "next_token": None,
            "synthetic": True,
            "observations": obs,
            "requested_range": {"start": start, "end": end},
            "uncovered_ranges": uncovered,
            "missing_sessions": missing,
        }
        return 200, require_valid(body, "api/read-snapshot-observations-response")

    # ================================================================ writes
    def _portfolio_guard(self, plan: Mapping[str, Any]) -> tuple[int, Any] | None:
        pf = self.portfolios.get(plan["portfolio_id"])
        if pf is not None and pf.get("synthetic") is not True:
            return self.error("OPERATION_NOT_PERMITTED", "phase 1 accepts writes to synthetic portfolios only", reason="non_synthetic_portfolio")
        return None

    def _create_version(self, request: Any, plan_id: str) -> tuple[int, Any]:
        body = request.body or {}

        def run() -> tuple[int, Any]:
            plan = self._plan(plan_id)
            if plan is None:
                return self.error("NOT_FOUND", "plan not found", record_type="plan")
            guard = self._portfolio_guard(plan)
            if guard:
                return guard
            parent = body.get("parent_plan_version_id")
            if parent not in self.versions or self.versions[parent]["plan_id"] != plan_id:
                return self.error("NOT_FOUND", "parent plan version not found in this plan", record_type="plan_version")
            if body.get("expected_revision") != plan["head"]["revision"]:
                return self.error("CONFLICT", "expected_revision does not match the current revision", expected_revision=body.get("expected_revision"), current_revision=plan["head"]["revision"])
            content = copy.deepcopy(body.get("content") or {})
            no_effect = canonicalize(content) == canonicalize(self.contents[parent])
            pv = self._new_version(plan_id, parent, content, origin="manual_override", status="pending_validation")
            plan["head"] = {"current_version_id": pv, "revision": plan["head"]["revision"] + 1}
            v = self.versions[pv]
            resp = {"plan_version_id": pv, "parent_plan_version_id": parent, "origin": "manual_override", "status": v["status"], "checksum": v["checksum"], "no_effect": no_effect, "revision": plan["head"]["revision"], "synthetic": True}
            return 201, require_valid(resp, "tools/create-override-version-response")

        return self.idempotent("create_plan_version", body, run)

    def _validate(self, request: Any, plan_version_id: str) -> tuple[int, Any]:
        body = request.body or {}

        def run() -> tuple[int, Any]:
            v = self.versions.get(plan_version_id)
            if v is None:
                return self.error("NOT_FOUND", "plan version not found", record_type="plan_version")
            alloc = self.contents[plan_version_id].get("allocation", {})
            total = sum(float(w["weight"]) for w in alloc.get("weights", [])) + float(alloc.get("cash_weight", 0))
            findings = [] if abs(total - 1.0) <= 1e-9 else [{"code": "VALIDATION_FAILED", "message": "weights plus cash do not sum to 1", "pointer": "/allocation"}]
            v["status"] = "validated" if not findings else "invalid"
            resp = {"plan_version_id": plan_version_id, "status": v["status"], "findings": findings, "synthetic": True}
            return 200, require_valid(resp, "tools/validate-plan-version-response")

        return self.idempotent("validate_plan_version", body, run)

    def _publish(self, request: Any, plan_id: str) -> tuple[int, Any]:
        body = request.body or {}

        def run() -> tuple[int, Any]:
            plan = self._plan(plan_id)
            if plan is None:
                return self.error("NOT_FOUND", "plan not found", record_type="plan")
            guard = self._portfolio_guard(plan)
            if guard:
                return guard
            pv = body.get("plan_version_id")
            v = self.versions.get(pv)
            if v is None or v["plan_id"] != plan_id:
                return self.error("NOT_FOUND", "plan version not found in this plan", record_type="plan_version")
            if body.get("expected_revision") != plan["head"]["revision"]:
                return self.error("CONFLICT", "expected_revision does not match the current revision", expected_revision=body.get("expected_revision"), current_revision=plan["head"]["revision"])
            if v["status"] != "validated":
                return self.error("PRECONDITION_FAILED", "only a validated version can be published", reason="not_validated", status=v["status"])
            pub_id = self.ids.mint("pub")
            pub = {
                "publication_id": pub_id,
                "plan_id": plan_id,
                "plan_version_id": pv,
                "plan_version_checksum": v["checksum"],
                "plan_version_status": "validated",
                "supersedes_publication_id": plan["current_publication_id"],
                "published_at": self.clock.iso(),
                "synthetic": True,
            }
            self.publications[pub_id] = require_valid(pub, "publication")
            plan["current_publication_id"] = pub_id
            plan["head"]["revision"] += 1
            return 201, require_valid(copy.deepcopy(pub), "tools/publish-plan-version-response")

        return self.idempotent("publish_plan_version", body, run)

    def _ingest(self, request: Any) -> tuple[int, Any]:
        body = request.body or {}

        def run() -> tuple[int, Any]:
            if body.get("granularity") not in self.declared_granularities:
                return self.error("VALIDATION_FAILED", "granularity is not declared by the provider", pointer="/granularity", declared=list(self.declared_granularities))
            if not str(body.get("dataset_id", "")).startswith("finance/etf-daily/"):
                return self.error("NOT_FOUND", "dataset is not configured", record_type="dataset")
            start, end = body["start_date"], body["end_date"]
            sid = self.add_snapshot(status="committed", start=start, end=end, quality_flags=self.ingestion_quality_flags, observed_end=self.ingestion_coverage_end)
            snap = copy.deepcopy(self.snapshots[sid])
            complete = self.ingestion_coverage_end is None
            resp = {
                "snapshot": snap,
                "requested_range": {"start": start, "end": end},
                "coverage_complete": complete,
                "input_snapshot_id": sid,
                "new_snapshot": True,
                "content_checksum": snap["manifest_checksum"],
                "quality_flags": list(snap["quality_flags"]),
                "trigger": "on_demand",
                "synthetic": True,
            }
            res = require_valid(resp, "tools/refresh-market-data-response")
            return 200, res

        return self.idempotent("run_ingestion", body, run)
