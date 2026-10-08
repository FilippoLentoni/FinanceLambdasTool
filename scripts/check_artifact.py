#!/usr/bin/env python3
"""Deployable-artifact content check (task 3.3, ENVW-05; with the D6 provider guard, MKT-10).

Fails when a Lambda bundle (a directory or a ``.zip``) contains:

* the test-only package ``finplan_tools_testing`` or any ``mock_*`` / fixture-minting module;
* test fixtures (``scenarios``, ``tests/``) or source markers of the mock producers
  (``class MockProducer``, ``class MockIds``);
* a market-data provider library (``yfinance``, ``exchange_calendars``) or an import of one: the
  tools never call a provider (the platform does);
* the deployable package ``finplan_tools`` is missing.

Usage: ``uv run python scripts/check_artifact.py <bundle-dir-or-zip>``; exit 1 on any problem.
"""

from __future__ import annotations

import re
import sys
import zipfile
from pathlib import Path
from typing import Iterator

__all__ = ["artifact_problems", "main", "FORBIDDEN_TOP_LEVEL", "PROVIDER_PACKAGES"]

FORBIDDEN_TOP_LEVEL = ("finplan_tools_testing", "tests", "testing")
PROVIDER_PACKAGES = ("yfinance", "exchange_calendars", "pandas_market_calendars", "yahooquery")
_MOCK_FILE = re.compile(r"(^|/)(mock_[A-Za-z0-9_]*|_?mocks?|scenarios|conftest)\.py\Z")
_MARKERS = (b"class MockProducer", b"class MockIds", b"class MockPlatform", b"class MockJobApi")
_TESTING_IMPORT = re.compile(rb"^\s*(?:from|import)\s+finplan_tools_testing\b", re.MULTILINE)
_PROVIDER_IMPORT = re.compile(rb"^\s*(?:from|import)\s+(" + b"|".join(p.encode() for p in PROVIDER_PACKAGES) + rb")\b", re.MULTILINE)


def _entries(path: Path) -> Iterator[tuple[str, bytes | None]]:
    if path.is_dir():
        for p in sorted(path.rglob("*")):
            if p.is_file():
                rel = p.relative_to(path).as_posix()
                yield rel, p.read_bytes() if p.suffix == ".py" else None
    elif zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as zf:
            for info in zf.infolist():
                if not info.is_dir():
                    yield info.filename, zf.read(info) if info.filename.endswith(".py") else None
    else:
        raise FileNotFoundError(f"{path} is neither a directory nor a zip file")


def artifact_problems(path: str | Path) -> list[str]:
    out: list[str] = []
    has_package = False
    for rel, data in _entries(Path(path)):
        top = rel.split("/", 1)[0]
        if rel.startswith("finplan_tools/"):
            has_package = True
        if top in FORBIDDEN_TOP_LEVEL:
            out.append(f"{rel}: test-only content in the deployable artifact")
            continue
        if top.split("-")[0] in PROVIDER_PACKAGES or top.replace("-", "_").split(".")[0] in PROVIDER_PACKAGES:
            out.append(f"{rel}: market-data provider library in the artifact")
            continue
        if _MOCK_FILE.search(rel) and (rel.startswith("finplan_tools/") or "/" not in rel):
            out.append(f"{rel}: mock or fixture-minting module in the artifact")
            continue
        if data is not None and rel.startswith("finplan_tools/"):
            if any(m in data for m in _MARKERS) or _TESTING_IMPORT.search(data):
                out.append(f"{rel}: mock producer code in the artifact")
            if _PROVIDER_IMPORT.search(data):
                out.append(f"{rel}: imports a market-data provider library")
    if not has_package:
        out.append("the artifact does not contain the finplan_tools package")
    return sorted(set(out))


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1:
        print("usage: check_artifact.py <bundle-dir-or-zip>", file=sys.stderr)
        return 2
    try:
        problems = artifact_problems(args[0])
    except (OSError, zipfile.BadZipFile) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    for p in problems:
        print(f"FAIL: {p}")
    print("PASS: artifact excludes mocks, fixtures and provider libraries" if not problems else f"FAIL: {len(problems)} artifact problems")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
