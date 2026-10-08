#!/usr/bin/env python3
"""Actions of each environment stage (tasks 9.1-9.3, 9.5; REL-05, REL-06; lesson L5). They run in the
per-environment stage project, as that environment's stage role, from the BuildOutput artifact only
(never the source checkout, never ``cdk synth``):

``predeploy`` (every environment, before ``DeployTools``)
    The contract pin must be allowed in the environment (``scripts/check_contracts_pin.py --env``:
    0.x is beta-only), then :func:`scripts.predeploy.resolve` (producer compatibility, per-call limit
    bound, same-environment reference resolution and principal-name validation). The resolved values
    are written to ``--variables-file`` for the buildspec to export as CodePipeline variables. Any
    problem stops the stage before anything is deployed.
``publish``
    :func:`scripts.release.publish_release`; prod first reads the manual approval (approver, time)
    of this pipeline execution.
``tests``
    The environment suite: ``integration-beta`` (beta) and ``gamma`` (gamma) run
    ``tests/integration``; ``smoke`` (prod) runs ``tests/smoke``. They make REAL SigV4 calls as the
    stage role (direct Lambda invocation of the deployed tools against the environment's deployed
    producers). ``FINPLAN_TARGET_ENV`` and ``FINPLAN_SUITE`` tell the tests where they run (and keep
    the offline credential harness away from them). Every suite must execute at least one test:
    zero executed tests is a false pass and fails (lesson L5).
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
for _p in (ROOT, ROOT / "src"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from scripts.release import ReleaseInfo, approval_record, publish_release

__all__ = ["SUITES", "main", "predeploy_action", "publish_action", "suite_counts", "tests_action"]

SUITES: dict[str, tuple[str, list[str]]] = {
    "beta": ("integration-beta", ["tests/integration"]),
    "gamma": ("gamma", ["tests/integration"]),
    "prod": ("smoke", ["tests/smoke"]),
}


def predeploy_action(env: str, *, ssm: Any, account: str | None, root: Path = ROOT, region: str | None = None, out: Callable[[str], None] = print) -> dict[str, str]:
    """Problems raise :class:`scripts.predeploy.PredeployError`; returns the CodePipeline variables."""
    from scripts.check_contracts_pin import check
    from scripts.predeploy import PredeployError, resolve

    pin = [f"contracts pin: {p}" for p in check(root, env=env, check_installed=False)]
    if pin:
        raise PredeployError(pin)
    kwargs = {"region": region} if region else {}
    result = resolve(env, ssm, account=account, **kwargs)
    for note in result.notes:
        out(f"NOTE: {note}")
    for k, v in sorted(result.variables.items()):
        out(f"{k}={v}")
    return result.variables


def publish_action(env: str, info: ReleaseInfo, *, ssm: Any, cfn: Any, s3: Any | None, codepipeline: Any | None, store: str | None, pipeline_name: str, execution_id: str | None) -> dict[str, Any]:
    approval = None
    if env == "prod":
        if codepipeline is None or not execution_id:
            raise RuntimeError("the prod manifest needs the pipeline execution ID to read the approval")
        approval = approval_record(codepipeline, pipeline_name, execution_id)
    return publish_release(info, env, ssm=ssm, cfn=cfn, s3=s3, store_bucket=store, approval=approval)


def suite_counts(junit_xml: Path) -> dict[str, int]:
    root = ET.parse(junit_xml).getroot()
    suites = [root] if root.tag == "testsuite" else list(root.iter("testsuite"))
    total = {k: 0 for k in ("tests", "failures", "errors", "skipped")}
    for s in suites:
        for k in total:
            total[k] += int(s.get(k, 0))
    total["executed"] = total["tests"] - total["skipped"]
    return total


def tests_action(env: str, *, root: Path = ROOT, release_id: str | None = None, run: Callable[..., Any] = subprocess.run, environ: Mapping[str, str] | None = None, out: Callable[[str], None] = print) -> int:
    suite, paths = SUITES[env]
    with tempfile.TemporaryDirectory() as tmp:
        junit = Path(tmp) / "junit.xml"
        env_vars = {**(environ if environ is not None else os.environ), "FINPLAN_TARGET_ENV": env, "FINPLAN_SUITE": suite}
        if release_id:
            env_vars["FINPLAN_RELEASE_ID"] = release_id
        proc = run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", f"--junitxml={junit}", *paths], cwd=root, env=env_vars)
        rc = int(getattr(proc, "returncode", 1))
        counts = suite_counts(junit) if junit.is_file() else {"tests": 0, "executed": 0, "failures": 0, "errors": 0, "skipped": 0}
    out(f"{suite}: {counts}")
    if rc == 5 or counts["executed"] < 1:
        out(f"FAIL: the {suite} suite executed no test (a stage with zero executed tests is a false pass)")
        return 1
    return 0 if rc == 0 else 1


def main(argv: list[str] | None = None) -> int:  # pragma: no cover - CodeBuild entry point (needs AWS)
    ap = argparse.ArgumentParser(description="FinanceLambdasTool stage actions: pre-deploy checks, release publishing or environment tests.")
    ap.add_argument("action", choices=("predeploy", "publish", "tests"))
    ap.add_argument("--env", required=True, choices=("beta", "gamma", "prod"))
    ap.add_argument("--release-info", type=Path, default=ROOT / "release-info.json")
    ap.add_argument("--pipeline-execution-id", default=None)
    ap.add_argument("--store", default=os.environ.get("FINPLAN_PIPELINE_STORE"))
    ap.add_argument("--variables-file", type=Path, default=None, help="predeploy: where to write the resolved CodePipeline variables")
    args = ap.parse_args(argv)
    info = ReleaseInfo.load(args.release_info)
    if args.action == "tests":
        return tests_action(args.env, release_id=info.release_id)
    import boto3

    from finplan_tools.core.aws_clients import s3_client, ssm_client
    from infra.stacks.naming import PIPELINE_NAME
    from scripts.predeploy import PredeployError, write_variables

    session = boto3.session.Session(region_name=info.region)
    ssm = ssm_client(info.region, boto_session=session)
    if args.action == "predeploy":
        try:
            account = session.client("sts").get_caller_identity()["Account"]
            variables = predeploy_action(args.env, ssm=ssm, account=account, region=info.region)
        except PredeployError as exc:
            for p in exc.problems:
                print(f"FAIL: {p}", file=sys.stderr)
            return 1
        if args.variables_file:
            write_variables(args.variables_file, variables)
        return 0
    manifest = publish_action(
        args.env,
        info,
        ssm=ssm,
        cfn=session.client("cloudformation"),
        s3=s3_client(info.region, boto_session=session),
        codepipeline=session.client("codepipeline"),
        store=args.store,
        pipeline_name=PIPELINE_NAME,
        execution_id=args.pipeline_execution_id,
    )
    print(f"published {args.env} manifest for {manifest['release_id']} (previous {manifest['previous_release_id']}; outputs {sorted(manifest['outputs'])})")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
