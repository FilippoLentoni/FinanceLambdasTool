#!/usr/bin/env python3
"""FinanceLambdasTool CDK app (task groups 1.1, 8 and 9).

Synthesis is offline and account-agnostic: no context lookups, no literal account or ARN. It builds
(:func:`infra.stacks.tools.add_to_app`):

* one stage per environment holding ``finplan-<env>-financelambdastool-tools`` (tool Lambdas, role
  classes, explicit invoke grants), synthesized with the pipeline-store synthesizer (no CDK bootstrap);
* the account-level ``finplan-shared-financelambdastool-pipeline-store`` (``LegacyStackSynthesizer``)
  and ``finplan-shared-financelambdastool-tooling`` (``CliCredentialsStackSynthesizer`` into the
  store) with the pipeline, deployed only by ``scripts/bootstrap.py`` (lesson L1).

Select environments with ``-c envs=beta,gamma`` (the pipeline needs all three); ``-c release_id=rel_...``
sets ``FINPLAN_RELEASE_ID`` on the functions (the build stage passes it). If ``infra.stacks.tools`` is
absent, each environment gets a resource-free skeleton stack instead.
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for _p in (ROOT, ROOT / "src"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import aws_cdk as cdk

from finplan_tools.core.config import ENVIRONMENTS, REGION


def build(app: cdk.App) -> list[cdk.Stack]:
    raw = app.node.try_get_context("envs")
    envs = [e for e in (raw.split(",") if isinstance(raw, str) and raw else ENVIRONMENTS) if e in ENVIRONMENTS]
    try:
        tools = importlib.import_module("infra.stacks.tools")
    except ModuleNotFoundError as exc:
        if exc.name != "infra.stacks.tools":
            raise
        tools = None
    if tools is not None:
        return list(tools.add_to_app(app, envs))
    stacks = []
    for env in envs:
        st = cdk.Stack(app, f"finplan-{env}-financelambdastool-skeleton", env=cdk.Environment(region=REGION), synthesizer=cdk.LegacyStackSynthesizer())
        cdk.Tags.of(st).add("environment", env)
        cdk.Tags.of(st).add("owner-repo", "financelambdastool")
        cdk.Tags.of(st).add("project", "finplan")
        stacks.append(st)
    return stacks


def main() -> None:
    app = cdk.App()
    build(app)
    app.synth()


if __name__ == "__main__":
    main()
