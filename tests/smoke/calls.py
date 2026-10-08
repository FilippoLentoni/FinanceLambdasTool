"""The prod smoke calls (task 9.5; REL-08): ``describe_capabilities`` and read-only tools only, with the
contract package's synthetic fixture requests. No write tool, no run, no publication. The same list is
replayed offline against the mock producers to prove zero write calls (``tests/unit/test_infra_smoke.py``)."""

from __future__ import annotations

from finplan_tools.core.registry import CATALOG

#: Tools the prod smoke stage invokes (read-only; the prod smoke role has no grant on any other tool).
SMOKE_TOOLS = ("describe_capabilities", "get_plan", "get_plan_version", "list_plan_versions", "query_market_data", "get_job_status", "get_experiment_result")

assert all(not CATALOG[t].state_changing for t in SMOKE_TOOLS), "the prod smoke suite is read-only"
