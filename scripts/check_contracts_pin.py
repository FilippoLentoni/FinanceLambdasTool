#!/usr/bin/env python3
"""Verify (or deliberately change) the finplan-contracts pin (task 1.2; contracts CS-04 consumer case).

The check fails ("Digest mismatch") unless ALL of these agree:

1. ``contracts-pin.json``: package, exact version and SHA-256 of the pinned wheel;
2. the vendored wheel on disk hashes to that SHA-256 (with the contract package's own
   ``finplan_contracts.digests.verify`` when it is importable);
3. ``pyproject.toml`` depends on ``finplan-contracts==<version>`` exactly (no range) and
   ``[tool.uv.sources]`` points at the pinned artifact;
4. ``uv.lock`` locks that version with the same wheel hash (uv refuses a wheel whose hash differs);
5. the installed distribution has that version (when installed).

There is no ``--rebuild``: FinanceLambdasTool never holds the contract sources (CS-01). ``--env
gamma|prod`` fails for a 0.x pin (0.x is beta-only).

``--repin [--from DIR_OR_WHEEL]`` is the one-command re-pin after a contract release: it copies the
wheel (default: the newest wheel in ``default_repin_source`` of the pin file, the sibling
FinancialPlanning checkout's ``vendor/finplan-contracts``), replaces the vendored artifact, rewrites
``contracts-pin.json`` and the exact pin in ``pyproject.toml``, and runs ``uv lock`` so the lock
records the new digest. It is a human/agent action, never a build step. Run ``uv sync`` next.

Exit codes: 0 ok, 1 mismatch, 2 usage/config error. Runs offline.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = "finplan-contracts"
_WHEEL_RE = re.compile(r"^finplan_contracts-(?P<version>[0-9][0-9A-Za-z.+-]*)-py3-none-any\.whl$")


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def load_pin(root: Path = ROOT, pin_path: Path | None = None) -> dict:
    return json.loads((pin_path or root / "contracts-pin.json").read_text(encoding="utf-8"))


def check(root: Path = ROOT, *, env: str | None = None, pin_path: Path | None = None, check_installed: bool = True) -> list[str]:
    problems: list[str] = []
    pin = load_pin(root, pin_path)
    package, version, digest = pin["package"], pin["version"], pin["sha256"].lower().removeprefix("sha256:")
    if package != PACKAGE:
        return [f"contracts-pin.json names {package!r}, expected {PACKAGE!r}"]
    if not re.fullmatch(r"[0-9a-f]{64}", digest):
        return ["contracts-pin.json: sha256 must be 64 lowercase hex characters"]
    wheel = (root / pin["artifact"]).resolve()
    if not wheel.is_file():
        return [f"pinned artifact {pin['artifact']} is missing"]

    try:
        from finplan_contracts.digests import verify

        ok = verify(wheel, digest)
    except ImportError:  # pragma: no cover - before the package is installed
        ok = _sha256(wheel) == digest
    if not ok:
        problems.append(f"Digest mismatch: {wheel.name} sha256 {_sha256(wheel)} != pinned {digest}")
    if f"-{version}-" not in wheel.name:
        problems.append(f"artifact {wheel.name} does not carry the pinned version {version}")

    pyproject = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    deps = pyproject.get("project", {}).get("dependencies", [])
    spec = next((d for d in deps if re.match(rf"^{re.escape(package)}\s*[=<>!~]", d)), None)
    if spec is None:
        problems.append(f"pyproject.toml does not depend on {package}")
    elif re.sub(r"\s", "", spec) != f"{package}=={version}":
        problems.append(f"pyproject.toml must pin {package}=={version} exactly (found {spec!r}); ranges are not allowed")
    src = pyproject.get("tool", {}).get("uv", {}).get("sources", {}).get(package, {})
    if not src.get("path"):
        problems.append(f"[tool.uv.sources] has no path for {package}; it must point at the pinned artifact")
    elif Path(src["path"]).as_posix() != Path(pin["artifact"]).as_posix():
        problems.append(f"[tool.uv.sources] {package} points at {src['path']}, not the pinned artifact")

    lock_path = root / "uv.lock"
    if lock_path.is_file():
        lock = tomllib.loads(lock_path.read_text(encoding="utf-8"))
        entry = next((p for p in lock.get("package", []) if p.get("name") == package), None)
        if entry is None:
            problems.append(f"uv.lock has no {package} entry")
        else:
            if entry.get("version") != version:
                problems.append(f"uv.lock locks {package} {entry.get('version')}, pin says {version}")
            hashes = {w.get("hash", "").removeprefix("sha256:") for w in entry.get("wheels", [])}
            if digest not in hashes:
                problems.append(f"Digest mismatch: uv.lock wheel hash {sorted(hashes)} does not include the pinned digest")
    else:
        problems.append("uv.lock is missing")

    if check_installed:
        from importlib.metadata import PackageNotFoundError, version as dist_version

        try:
            installed = dist_version(package)
            if installed != version:
                problems.append(f"installed {package} {installed} != pinned {version} (run 'uv sync')")
        except PackageNotFoundError:
            pass

    if env in ("gamma", "prod") and version.startswith("0."):
        problems.append(f"contract version {version} is a 0.x pre-release and may be deployed to beta only; {env} requires 1.0.0 or later")
    return problems


def _version_key(v: str) -> tuple:
    return tuple(int(p) if p.isdigit() else p for p in re.split(r"[.+-]", v))


def find_source_wheel(source: Path) -> Path:
    """The wheel to pin: ``source`` itself, or the newest finplan_contracts wheel in that directory."""
    if source.is_file():
        if not _WHEEL_RE.match(source.name):
            raise ValueError(f"{source.name} is not a finplan_contracts wheel")
        return source
    if not source.is_dir():
        raise FileNotFoundError(f"re-pin source {source} does not exist")
    wheels = [p for p in source.glob("*.whl") if _WHEEL_RE.match(p.name)]
    if not wheels:
        raise FileNotFoundError(f"no finplan_contracts wheel in {source}")
    return max(wheels, key=lambda p: _version_key(_WHEEL_RE.match(p.name)["version"]))  # type: ignore[index]


def repin(root: Path = ROOT, source: Path | None = None, *, run_lock: bool = True) -> tuple[str, str]:
    """Vendor ``source`` (default: the pin file's ``default_repin_source``) and re-pin; returns (version, digest)."""
    pin_file = root / "contracts-pin.json"
    pin = load_pin(root)
    src = source if source is not None else (root / pin.get("default_repin_source", "../FinancialPlanning/vendor/finplan-contracts"))
    wheel = find_source_wheel(src.resolve())
    version = _WHEEL_RE.match(wheel.name)["version"]  # type: ignore[index]
    dest_dir = root / "vendor" / "finplan-contracts"
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / wheel.name
    if wheel.resolve() != dest.resolve():
        for old in dest_dir.glob("*.whl"):
            old.unlink()
        shutil.copyfile(wheel, dest)
    digest = _sha256(dest)
    pin.update(version=version, sha256=digest, artifact=dest.relative_to(root).as_posix())
    pin["served_environments"] = ["beta"] if version.startswith("0.") else ["beta", "gamma", "prod"]
    pin_file.write_text(json.dumps(pin, indent=2) + "\n", encoding="utf-8")
    py = root / "pyproject.toml"
    text = py.read_text(encoding="utf-8")
    text = re.sub(r'"finplan-contracts==[^"]+"', f'"finplan-contracts=={version}"', text)
    text = re.sub(r'(finplan-contracts = \{ path = ")[^"]+(" \})', rf"\g<1>{dest.relative_to(root).as_posix()}\g<2>", text)
    py.write_text(text, encoding="utf-8")
    if run_lock:
        subprocess.run(["uv", "lock", "--upgrade-package", PACKAGE], cwd=root, check=True)
    return version, digest


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--root", type=Path, default=ROOT)
    ap.add_argument("--pin", type=Path, help="pin file (default: <root>/contracts-pin.json)")
    ap.add_argument("--env", choices=["beta", "gamma", "prod"], help="deployment target (0.x pins are beta-only)")
    ap.add_argument("--repin", action="store_true", help="vendor a contract wheel and re-pin (deliberate action, not a build step)")
    ap.add_argument("--from", dest="source", type=Path, help="--repin source: a wheel or a directory holding wheels")
    args = ap.parse_args(argv)
    try:
        if args.repin:
            version, digest = repin(args.root, args.source)
            print(f"re-pinned finplan-contracts {version}: sha256 {digest}; run 'uv sync' next")
            return 0
        problems = check(args.root, env=args.env, pin_path=args.pin)
    except (OSError, KeyError, ValueError, json.JSONDecodeError, subprocess.CalledProcessError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    if problems:
        for p in problems:
            print(f"FAIL: {p}")
        return 1
    print("PASS: finplan-contracts pin verified (version, wheel digest, pyproject, uv.lock)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
