#!/usr/bin/env python3
"""Build-stage gates of the FinanceLambdasTool pipeline (tasks 1.2, 1.3, 3.3, 7.6; contracts D6).

The build stage fails on any gate failure. Every contract gate reuses the pinned
``finplan-contracts`` package; nothing is copied or re-implemented.

====================  =====  ==================================================================
gate                  stage  what
====================  =====  ==================================================================
contracts-pin         pre    ``scripts/check_contracts_pin.py`` (version, wheel SHA-256, pyproject,
                             uv.lock); ``--env gamma|prod`` refuses a 0.x pin (CS-04)
config                pre    ``config/tool-limits.default.json`` is valid and equals the code
                             defaults; ``config/environments.json`` names beta/gamma/prod only
leak-scan             pre    ``finplan_contracts.leak_scan`` over the repository: account IDs,
                             ARNs with accounts, bucket names, endpoints, secrets (OWN-03)
copied-id             pre    ``finplan_contracts.copied_id``: no contract schema ``$id`` or schema
                             copy anywhere in the repository (CS-01)
conformance           pre    consumer-mode conformance of the installed package, at the pinned version
live-perm-scan        pre    ``finplan_contracts.live_perms`` over repository JSON/YAML (ENV-05)
tool-inventory        pre    the D1 catalog has no execute/trade/order/payment/wallet tool (PLN-08)
s3-client-guard       pre    no bare ``boto3.client("s3"...)`` outside ``core/aws_clients.py`` (L4)
provider-isolation    pre    no market-data provider library, import, secret read or provider host in
                             ``src/`` or the dependencies (MKT-10; ``check_provider_isolation.py``)
fixture-provenance    pre    market-data fixtures and scenarios are synthetic with mock-provider
                             lineage (MKT-09; public repository)
artifact              post   with ``--bundle``: ``scripts/check_artifact.py`` (no mocks, fixtures or
                             provider libraries in the deployable bundle; ENVW-05, MKT-10)
====================  =====  ==================================================================

Other task groups (infrastructure: synth, ownership, boundaries, cost, bundle architecture,
pipeline structure) append gates to :data:`GATES` as ``(name, stage, function)``; a function takes a
:class:`GateContext` and returns a list of problems.

Usage: ``uv run python scripts/build_gates.py --stage pre|post|all [--env ENV] [--bundle PATH]
[--assembly cdk.out] [--only GATE ...]``; exit 1 when any gate fails. Runs offline.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import re
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for _p in (ROOT, ROOT / "src"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

__all__ = ["GATES", "GateContext", "GateResult", "run_gates", "main", "EXTRA_EXCLUDES"]

#: Directories never scanned in addition to the contract defaults (build outputs, local caches,
#: the vendored contract wheel, editor/agent configuration).
EXTRA_EXCLUDES = frozenset({".build", "build-output", "cdk.out", "cdk.out.bootstrap", "vendor", ".claude"})


@dataclass
class GateContext:
    root: Path = ROOT
    env: str | None = None
    bundle: Path | None = None
    assembly: Path | None = None
    notes: dict[str, list[str]] = field(default_factory=dict)

    def note(self, gate: str, text: str) -> None:
        self.notes.setdefault(gate, []).append(text)


@dataclass
class GateResult:
    name: str
    problems: list[str]
    notes: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.problems


def _call_main(fn: Callable[[list[str]], int], argv: list[str]) -> tuple[int, str]:
    out = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
        try:
            rc = fn(argv)
        except SystemExit as exc:
            rc = int(exc.code or 0)
    return rc, out.getvalue()[-4000:]


def _excludes() -> frozenset[str]:
    from finplan_contracts import leak_scan

    return frozenset(leak_scan.DEFAULT_EXCLUDE_DIRS | EXTRA_EXCLUDES)


# ===================================================================== pre gates
def gate_contracts_pin(ctx: GateContext) -> list[str]:
    from scripts.check_contracts_pin import check

    return [f"contracts pin: {p}" for p in check(ctx.root, env=ctx.env)]


def gate_config(ctx: GateContext) -> list[str]:
    from finplan_tools.core.config import DEFAULT_TOOL_LIMITS, ENVIRONMENTS, tool_limits_problems

    out: list[str] = []
    try:
        limits = json.loads((ctx.root / "config" / "tool-limits.default.json").read_text(encoding="utf-8"))
        envs = json.loads((ctx.root / "config" / "environments.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return [f"config: {exc}"]
    out += [f"config/tool-limits.default.json: {p}" for p in tool_limits_problems(limits)]
    if limits != DEFAULT_TOOL_LIMITS:
        out.append("config/tool-limits.default.json differs from finplan_tools.core.config.DEFAULT_TOOL_LIMITS")
    if sorted(envs.get("environments", {})) != sorted(ENVIRONMENTS):
        out.append("config/environments.json must define exactly beta, gamma and prod")
    for env, cfg in envs.get("environments", {}).items():
        if cfg.get("log_retention_days") != 30:
            out.append(f"config/environments.json: {env} log_retention_days must be 30")
    if envs.get("environments", {}).get("prod", {}).get("direct_test_write_tools") is not False:
        out.append("config/environments.json: prod direct-test grants must cover read-only tools only")
    return out


def gate_leak_scan(ctx: GateContext) -> list[str]:
    from finplan_contracts import leak_scan

    n, findings = leak_scan.scan_paths([ctx.root], exclude_dirs=_excludes())
    ctx.note("leak-scan", f"{n} files scanned")
    return [f"leak: {f}" for f in findings]


def gate_copied_id(ctx: GateContext) -> list[str]:
    from finplan_contracts import copied_id

    found = copied_id.scan_tree(ctx.root, exclude_dirs=copied_id.EXCLUDE_DIRS | EXTRA_EXCLUDES)
    return [f"copied contract schema: {c}" for c in found]


def gate_conformance(ctx: GateContext) -> list[str]:
    from finplan_contracts import conformance

    from scripts.check_contracts_pin import load_pin

    version = load_pin(ctx.root)["version"]
    rc, out = _call_main(conformance.main, ["--mode", "consumer", "--expect-version", version])
    if rc == 0:
        return []
    lines = out.strip().splitlines()
    return ["conformance (consumer mode) failed: " + (lines[-1] if lines else "no output")]


def gate_live_perm_scan(ctx: GateContext) -> list[str]:
    from finplan_contracts import live_perms

    excludes = _excludes() | {"tests", "testing"}
    paths = [p for p in ctx.root.rglob("*") if p.is_file() and p.suffix in (".json", ".yaml", ".yml") and not any(part in excludes for part in p.relative_to(ctx.root).parts)]
    _, findings = live_perms.scan_paths(paths)
    return [f"live permission: {f}" for f in findings]


def gate_tool_inventory(ctx: GateContext) -> list[str]:
    from finplan_tools.core.registry import CATALOG, inventory_problems

    return [f"tool inventory: {p}" for p in inventory_problems(CATALOG)]


_BARE_S3 = re.compile(r"""\.client\(\s*["']s3["']""")


def gate_s3_client_guard(ctx: GateContext) -> list[str]:
    """Real-deploy lesson L4: S3 clients only through ``core/aws_clients.s3_client`` (SigV4, regional)."""
    allowed = {ctx.root / "src" / "finplan_tools" / "core" / "aws_clients.py", Path(__file__).resolve()}
    out = []
    for p in sorted((ctx.root / "src").rglob("*.py")) + sorted((ctx.root / "infra").rglob("*.py")) + sorted((ctx.root / "scripts").rglob("*.py")):
        if p.resolve() in allowed:
            continue
        if _BARE_S3.search(p.read_text(encoding="utf-8")):
            out.append(f"{p.relative_to(ctx.root)}: bare S3 client; use finplan_tools.core.aws_clients.s3_client")
    return out


def gate_provider_isolation(ctx: GateContext) -> list[str]:
    """MKT-10: no provider library, provider import, secret read or provider host in the tool source."""
    from scripts.check_provider_isolation import source_problems

    return [f"provider isolation: {p}" for p in source_problems(ctx.root)]


def gate_fixture_provenance(ctx: GateContext) -> list[str]:
    """MKT-09: market-data fixtures are synthetic with a mock-provider lineage (public repository)."""
    from scripts.check_provider_isolation import json_fixture_problems, scenario_problems

    problems = json_fixture_problems(ctx.root)
    if ctx.root == ROOT:
        problems += scenario_problems()
    return [f"fixture provenance: {p}" for p in problems]


# ===================================================================== post gates
def gate_artifact(ctx: GateContext) -> list[str]:
    if ctx.bundle is None:
        ctx.note("artifact", "skipped: no --bundle given")
        return []
    from scripts.check_artifact import artifact_problems

    return [f"artifact: {p}" for p in artifact_problems(ctx.bundle)]


GATES: list[tuple[str, str, Callable[[GateContext], list[str]]]] = [
    ("contracts-pin", "pre", gate_contracts_pin),
    ("config", "pre", gate_config),
    ("leak-scan", "pre", gate_leak_scan),
    ("copied-id", "pre", gate_copied_id),
    ("conformance", "pre", gate_conformance),
    ("live-perm-scan", "pre", gate_live_perm_scan),
    ("tool-inventory", "pre", gate_tool_inventory),
    ("s3-client-guard", "pre", gate_s3_client_guard),
    ("provider-isolation", "pre", gate_provider_isolation),
    ("fixture-provenance", "pre", gate_fixture_provenance),
    ("artifact", "post", gate_artifact),
]

# Infrastructure post-synth gates (task groups 8 and 9; they need --assembly).
from scripts.infra_gates import INFRA_GATES  # noqa: E402

GATES += INFRA_GATES


def run_gates(ctx: GateContext, stage: str = "all", only: list[str] | None = None) -> list[GateResult]:
    results = []
    for name, st, fn in GATES:
        if stage != "all" and st != stage:
            continue
        if only and name not in only:
            continue
        try:
            problems = fn(ctx)
        except Exception as exc:  # noqa: BLE001 - a crashing gate is a failing gate
            problems = [f"{name}: gate crashed: {type(exc).__name__}: {exc}"]
        results.append(GateResult(name, problems, ctx.notes.get(name, [])))
    return results


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--stage", choices=["pre", "post", "all"], default="all")
    ap.add_argument("--env", choices=["beta", "gamma", "prod"])
    ap.add_argument("--bundle", type=Path, help="Lambda bundle (dir or zip) for the artifact gate")
    ap.add_argument("--assembly", type=Path, help="synthesized cloud assembly (post gates of other task groups)")
    ap.add_argument("--only", nargs="*")
    args = ap.parse_args(argv)
    results = run_gates(GateContext(env=args.env, bundle=args.bundle, assembly=args.assembly), args.stage, args.only)
    failed = False
    for r in results:
        print(f"{'PASS' if r.ok else 'FAIL'}: {r.name}" + (f" ({'; '.join(r.notes)})" if r.notes else ""))
        for p in r.problems:
            print(f"  - {p}")
        failed |= not r.ok
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
