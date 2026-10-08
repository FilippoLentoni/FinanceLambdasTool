#!/usr/bin/env python3
"""Pipeline synthesis of the cloud assembly (task 9.3; contracts D6 "cloud assembly built once").

Same app as ``infra/app.py``: the environment stacks use
:func:`infra.stacks.tooling.deployment_synthesizer` (Lambda code asset in the FinanceLambdasTool
pipeline store under ``assets/<sha256>.zip``, no bootstrap-version rule, no CDK role), so the
pipeline's CloudFormation actions deploy them without a ``CDKToolkit`` stack; the account-level stacks
keep their own synthesizers (store: legacy, inline; tooling: CLI-credentials staging in the store).

Usage (offline): ``uv run python scripts/synth.py --out cdk.out``. That local form packages the
``src/`` tree (fine for tests and the bootstrap, NOT deployable for the Lambdas). ``--release``
reproduces the build stage: it builds the arm64 Lambda bundle (:mod:`scripts.lambda_bundle`) and
synthesizes in release mode, where a missing bundle fails the synth (lesson L3).
"""

from __future__ import annotations

import argparse
import contextlib
import os
import sys
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
for _p in (ROOT, ROOT / "src"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import aws_cdk as cdk

from infra.app import build

__all__ = ["release_environment", "synth"]


def synth(outdir: str | os.PathLike[str] | None = None, *, release_id: str | None = None, context: Mapping[str, Any] | None = None) -> Path:
    """Synthesize the full app (all environments, store, tooling, pipeline); returns the assembly directory."""
    out = Path(outdir or os.environ.get("CDK_OUTDIR") or ROOT / "cdk.out")
    ctx = dict(context or {})
    if release_id:
        ctx["release_id"] = release_id
    app = cdk.App(outdir=str(out), context=ctx)
    build(app)
    return Path(app.synth().directory)


@contextlib.contextmanager
def release_environment(bundle: Path) -> Iterator[None]:
    """``FINPLAN_RELEASE_BUILD=1`` and ``FINPLAN_LAMBDA_BUNDLE_DIR`` for the duration of the synth."""
    from infra.stacks.lambda_code import BUNDLE_DIR_ENV, RELEASE_ENV

    saved = {k: os.environ.get(k) for k in (BUNDLE_DIR_ENV, RELEASE_ENV)}
    os.environ[BUNDLE_DIR_ENV] = str(bundle)
    os.environ[RELEASE_ENV] = "1"
    try:
        yield
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Synthesize the FinanceLambdasTool cloud assembly (offline).")
    ap.add_argument("--out", help="assembly directory (default: $CDK_OUTDIR or ./cdk.out)")
    ap.add_argument("--release", action="store_true", help="build the arm64 Lambda bundle and synthesize in release mode (as the build stage does)")
    ap.add_argument("--bundle", type=Path, default=ROOT / ".build" / "lambda-bundle", help="bundle directory for --release")
    args = ap.parse_args(argv)
    if not args.release:
        print(synth(args.out))
        return 0
    from scripts.lambda_bundle import build_bundle

    manifest = build_bundle(ROOT, args.bundle, replace=True)
    print(f"lambda bundle: {manifest['unzipped_bytes'] / 2**20:.1f} MiB unzipped, {manifest['files']} files", file=sys.stderr)
    with release_environment(args.bundle):
        print(synth(args.out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
