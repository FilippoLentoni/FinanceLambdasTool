"""Provider isolation and fixture provenance build checks (task 5.2b; MKT-09, MKT-10).

Negative inputs are generated in ``tmp_path`` (never committed: the repository is public).
"""

from __future__ import annotations

import json

import pytest

from scripts.build_gates import GATES, GateContext, run_gates
from scripts.check_provider_isolation import document_problems, json_fixture_problems, scenario_problems, source_problems
from finplan_tools_testing.runtime import offline_runtime
from finplan_tools_testing.scenarios import REAL_PROVIDER_LINEAGE_ALLOWED, SCENARIOS


def _tree(tmp_path, files):
    for rel, text in files.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    return tmp_path


def test_gates_registered_pre_stage():
    names = {n: st for n, st, _ in GATES}
    assert names["provider-isolation"] == "pre" and names["fixture-provenance"] == "pre"


def test_repository_passes_both_checks():
    results = run_gates(GateContext(), "pre", only=["provider-isolation", "fixture-provenance"])
    assert [r.problems for r in results] == [[], []]


def test_clean_tree_passes(tmp_path):
    root = _tree(tmp_path, {"src/pkg/a.py": "import json\n", "pyproject.toml": 'dependencies = ["jsonschema==4.0"]\n'})
    assert source_problems(root) == []


@pytest.mark.parametrize(
    "src",
    [
        "import yfinance as yf\n",
        "from exchange_calendars import get_calendar\n",
        "mod = importlib.import_module('yfinance')\n",
        "c = boto3.client('secretsmanager')\n",
        "ssm.get_parameter(Name=n, WithDecryption=True)\n",
        "key = os.environ['YFINANCE_API_KEY']\n",
        "URL = 'query1.finance.yahoo.com'\n",
    ],
)
def test_mkt10_source_negative(tmp_path, src):
    root = _tree(tmp_path, {"src/pkg/tool.py": src})
    assert source_problems(root)


def test_mkt10_dependency_negative(tmp_path):
    root = _tree(tmp_path, {"src/pkg/a.py": "", "pyproject.toml": 'dependencies = ["yfinance==1.7.0"]\n'})
    assert any("pyproject" in p for p in source_problems(root))


def test_mkt09_json_fixture_naming_real_provider_fails(tmp_path):
    snap = {"input_snapshot_id": "snap_x", "lineage": {"provider": "yfinance", "retrieved_at": "2026-01-10T13:00:00Z"}, "synthetic": True}
    root = _tree(tmp_path, {"tests/fixtures/md/real.json": json.dumps(snap)})
    assert any("yfinance" in p for p in json_fixture_problems(root))


def test_mkt09_json_fixture_not_synthetic_fails(tmp_path):
    snap = {"lineage": {"provider": "fixture"}}
    obs = [{"instrument_id": "SPY", "session_date": "2026-01-02", "kind": "completed_daily", "close": 1.0}]
    root = _tree(tmp_path, {"testing/a.json": json.dumps(snap), "tests/b.json": json.dumps(obs)})
    problems = json_fixture_problems(root)
    assert len(problems) == 2


def test_mkt09_mock_fixture_passes(tmp_path):
    snap = {"lineage": {"provider": "mock"}, "synthetic": True, "observations": [{"instrument_id": "SPY", "session_date": "2026-01-02", "close": 1.0, "synthetic": True}]}
    root = _tree(tmp_path, {"tests/ok.json": json.dumps(snap)})
    assert json_fixture_problems(root) == []


def test_mkt09_scenario_with_real_lineage_needs_allow_list():
    shape = {"md_yfinance_lineage_shape": SCENARIOS["md_yfinance_lineage_shape"]}
    assert scenario_problems(shape, allowed=REAL_PROVIDER_LINEAGE_ALLOWED, world=lambda: offline_runtime("beta")) == []
    assert scenario_problems(shape, allowed=(), world=lambda: offline_runtime("beta"))


def test_mkt09_allow_listed_scenario_still_needs_synthetic_observations():
    def leaky(o):
        sid = SCENARIOS["md_yfinance_lineage_shape"](o).ids["input_snapshot_id"]
        o.platform.observations[sid][0]["synthetic"] = False
        return SCENARIOS["md_partial_response"](o)

    problems = scenario_problems({"md_yfinance_lineage_shape": leaky}, allowed=REAL_PROVIDER_LINEAGE_ALLOWED, world=lambda: offline_runtime("beta"))
    assert any("observation without synthetic" in p for p in problems)


def test_document_problems_nested():
    assert document_problems({"a": [{"lineage": {"provider": "yahooquery"}, "synthetic": True}]}, "x")
