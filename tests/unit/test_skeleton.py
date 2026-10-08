"""Task 1.1: the offline suite runs and the CDK skeleton synthesizes offline (no lookups, no account)."""

from __future__ import annotations

import json
import os
import shutil

import pytest

pytestmark = pytest.mark.synth


@pytest.mark.skipif(shutil.which("node") is None, reason="CDK synth needs node (the toolchain prefix loads nvm)")
def test_skeleton_synthesizes_offline(tmp_path):
    import aws_cdk as cdk

    from infra.app import build

    app = cdk.App(outdir=str(tmp_path))
    stacks = build(app)
    asm = app.synth()
    names = {s.stack_name for s in asm.stacks}
    assert names >= {f"finplan-{e}-financelambdastool-skeleton" for e in ("beta", "gamma", "prod")} or len(stacks) >= 3
    for s in asm.stacks:
        tpl = json.loads((tmp_path / s.template_file).read_text())
        text = json.dumps(tpl)
        assert "cdk-hnb659fds" not in text and "BootstrapVersion" not in text  # L1
    assert os.environ.get("AWS_ACCESS_KEY_ID") == "testing-fake-key"
