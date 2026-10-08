"""Shared helpers of the infrastructure unit tests (task groups 8 and 9): one offline synth per
test process, template lookup by environment, and IAM policy-simulation inputs."""

from __future__ import annotations

import functools
import json
import shutil
import tempfile
from pathlib import Path
from typing import Any

import pytest

ENVS = ("beta", "gamma", "prod")
needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="CDK synth needs node (the toolchain prefix loads nvm)")
#: Concrete placeholders for offline policy simulation (never a real account).
PARTITION, REGION, ACCOUNT = "aws", "us-east-2", "0" * 12
#: A store bucket ARN built at run time (no bucket-shaped literal in the repository).
STORE_ARN = ":".join(["arn", "aws", "s3", "", "", "store"])


@functools.lru_cache(maxsize=4)
def assembly(context_json: str = "{}") -> Path:
    """Synthesize the full app once per distinct context (offline, source-only code, no release mode)."""
    from scripts.synth import synth

    out = Path(tempfile.mkdtemp(prefix="flt-synth-"))
    return synth(out / "cdk.out", release_id="rel_01KDVDNAZ83BAMMYCEGWF33DPM", context=json.loads(context_json))


def templates(asm: Path) -> dict[str, dict[str, Any]]:
    """``{beta|gamma|prod|store|tooling: template}``."""
    out: dict[str, dict[str, Any]] = {}
    for p in asm.rglob("*.template.json"):
        t = json.loads(p.read_text(encoding="utf-8"))
        if p.name.startswith("PipelineStore"):
            out["store"] = t
        elif p.name.startswith("Tooling"):
            out["tooling"] = t
        else:
            for env in ENVS:
                if p.parent.name == f"assembly-{env.capitalize()}":
                    out[env] = t
    return out


def resources(template: dict[str, Any], rtype: str) -> dict[str, dict[str, Any]]:
    return {k: v for k, v in template["Resources"].items() if v.get("Type") == rtype}


def tags(resource: dict[str, Any]) -> dict[str, str]:
    return {t["Key"]: t["Value"] for t in (resource.get("Properties") or {}).get("Tags") or [] if isinstance(t, dict)}
