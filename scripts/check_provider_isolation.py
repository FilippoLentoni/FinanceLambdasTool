#!/usr/bin/env python3
"""Market-data provider isolation and fixture provenance (task 5.2b; MKT-09, MKT-10; design D6).

Two build checks, both offline:

* :func:`source_problems` - the tool source (``src/``) and the project dependencies name no
  market-data provider library (``yfinance``, ``exchange_calendars``, ...), import none, read no
  secret (Secrets Manager, decrypted SecureString parameters, provider API-key variables) and name no
  provider host. Tools reach market data only through the platform API.
* :func:`fixture_problems` - this repository is public and the phase 2 provider's terms are
  personal/research use, so no retrieved market data may be committed. Every market-data fixture is
  synthetic with a mock-provider lineage:

  - each JSON file under ``tests/`` and ``testing/`` that carries a snapshot ``lineage`` or market
    observations must be ``synthetic: true`` and name a mock provider;
  - each synthetic scenario of :mod:`finplan_tools_testing.scenarios` is seeded into an offline
    world and every snapshot and observation it creates is checked the same way. The single
    exception is a scenario listed in ``REAL_PROVIDER_LINEAGE_ALLOWED`` (lineage *shape* test): its
    lineage may name a real provider, but its observations must still be synthetic.

  This check covers committed files only. Deployed platform data is a different matter: beta, gamma
  and (after its transition) prod serve real phase 2 snapshots (decision 26, data parity), and the
  deployed suites accept real or synthetic platform data (``tests/deployed_support.py``).

Usage: ``uv run python scripts/check_provider_isolation.py [--root DIR]``; exit 1 on any problem.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Mapping

ROOT = Path(__file__).resolve().parents[1]

PROVIDER_PACKAGES = ("yfinance", "exchange_calendars", "pandas_market_calendars", "yahooquery", "alpha_vantage", "polygon", "alpaca_trade_api")
MOCK_PROVIDERS = ("fixture", "mock")
_PROVIDER_IMPORT = re.compile(r"^\s*(?:from|import)\s+(" + "|".join(PROVIDER_PACKAGES) + r")\b", re.MULTILINE)
_PROVIDER_MENTION = re.compile(r"""(?:importlib\.import_module|__import__)\(\s*["'](""" + "|".join(PROVIDER_PACKAGES) + r")")
_SECRET_READ = re.compile(r"""secretsmanager|get_secret_value|SecretString|WithDecryption\s*=\s*True|\b[A-Z_]*(?:PROVIDER|YAHOO|YFINANCE)[A-Z_]*_(?:KEY|TOKEN|SECRET)\b""")
_PROVIDER_HOST = re.compile(r"(?i)\b(?:query[12]\.)?finance\.yahoo\.com\b|\byahooapis\.com\b")


def source_problems(root: Path = ROOT) -> list[str]:
    out: list[str] = []
    for p in sorted((root / "src").rglob("*.py")):
        text = p.read_text(encoding="utf-8")
        rel = p.relative_to(root)
        for rx, what in ((_PROVIDER_IMPORT, "imports market-data provider library"), (_PROVIDER_MENTION, "loads market-data provider library"), (_SECRET_READ, "reads a secret"), (_PROVIDER_HOST, "names a provider host")):
            m = rx.search(text)
            if m:
                out.append(f"{rel}: {what} ({m.group(0).strip()[:60]})")
    pyproject = root / "pyproject.toml"
    if pyproject.exists():
        deps = pyproject.read_text(encoding="utf-8").lower().replace("-", "_")
        for pkg in PROVIDER_PACKAGES:
            if re.search(rf"""["']{pkg}\b""", deps):
                out.append(f"pyproject.toml: depends on market-data provider library {pkg}")
    return out


# ------------------------------------------------------------------ fixture provenance
def _walk(doc: Any) -> Iterator[Mapping[str, Any]]:
    if isinstance(doc, Mapping):
        yield doc
        for v in doc.values():
            yield from _walk(v)
    elif isinstance(doc, list):
        for v in doc:
            yield from _walk(v)


def _is_observation(d: Mapping[str, Any]) -> bool:
    return "session_date" in d and ("close" in d or "kind" in d)


def document_problems(doc: Any, where: str, *, lineage_allowed: bool = False) -> list[str]:
    """Problems of one market-data document (snapshot, observation list, any nesting)."""
    out: list[str] = []
    for d in _walk(doc):
        lineage = d.get("lineage")
        if isinstance(lineage, Mapping) and "provider" in lineage:
            if d.get("synthetic") is not True:
                out.append(f"{where}: market-data record without synthetic: true")
            if lineage.get("provider") not in MOCK_PROVIDERS and not lineage_allowed:
                out.append(f"{where}: lineage names provider {lineage.get('provider')!r}; fixtures must use a mock provider")
        if _is_observation(d) and d.get("synthetic") is not True:
            out.append(f"{where}: observation without synthetic: true")
    return out


def json_fixture_problems(root: Path = ROOT, dirs: Iterable[str] = ("tests", "testing")) -> list[str]:
    out: list[str] = []
    for top in dirs:
        for p in sorted((root / top).rglob("*.json")) if (root / top).exists() else ():
            try:
                doc = json.loads(p.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            out += document_problems(doc, str(p.relative_to(root)))
    return out


def scenario_problems(scenarios: Mapping[str, Callable[[Any], Any]] | None = None, *, allowed: Iterable[str] | None = None, world: Callable[[], Any] | None = None) -> list[str]:
    """Seed every scenario offline and check every snapshot and observation it created."""
    if scenarios is None or allowed is None or world is None:
        for extra in (ROOT / "testing", ROOT / "src"):
            if str(extra) not in sys.path:
                sys.path.insert(0, str(extra))
        from finplan_tools_testing import scenarios as mod
        from finplan_tools_testing.runtime import offline_runtime

        scenarios = mod.SCENARIOS if scenarios is None else scenarios
        allowed = mod.REAL_PROVIDER_LINEAGE_ALLOWED if allowed is None else allowed
        world = world or (lambda: offline_runtime("beta"))
    allowed = frozenset(allowed)
    out: list[str] = []
    for name, build in scenarios.items():
        o = world()
        s = build(o)
        where = f"scenario {name}"
        out += document_problems(list(o.platform.snapshots.values()), where, lineage_allowed=name in allowed)
        out += document_problems(list(o.platform.observations.values()), where)
        out += document_problems([doc for _schema, doc in getattr(s, "documents", [])], where, lineage_allowed=name in allowed)
    return out


def fixture_problems(root: Path = ROOT) -> list[str]:
    return json_fixture_problems(root) + scenario_problems()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--root", type=Path, default=ROOT)
    args = ap.parse_args(argv)
    problems = source_problems(args.root) + json_fixture_problems(args.root) + (scenario_problems() if args.root == ROOT else [])
    for p in problems:
        print(f"FAIL {p}")
    print("provider isolation and fixture provenance: " + ("FAIL" if problems else "ok"))
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
