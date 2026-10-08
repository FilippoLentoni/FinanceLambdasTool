"""Lambda bundle (lesson L3) and the build stage (task 9.3: a failing gate produces no artifact;
REL-04 release-info). Offline: the uv install is replaced by a fake runner."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import build_stage, lambda_bundle

ROOT = Path(__file__).resolve().parents[2]


def _elf(machine: int) -> bytes:
    head = bytearray(b"\x7fELF" + bytes([2, 1, 1]) + bytes(11))
    head += machine.to_bytes(2, "little")
    return bytes(head) + bytes(64)


def fake_runner(packages: list[str], *, so_machine: int | None = None):
    def run(cmd: list[str], cwd: Path) -> str:
        if "--target" in cmd:
            dest = Path(cmd[cmd.index("--target") + 1])
            for pkg in packages:
                (dest / pkg).mkdir(parents=True, exist_ok=True)
                (dest / pkg / "__init__.py").write_text("")
            if so_machine is not None:
                (dest / "rpds" / "rpds.cpython-312-aarch64-linux-gnu.so").parent.mkdir(exist_ok=True)
                (dest / "rpds" / "rpds.cpython-312-aarch64-linux-gnu.so").write_bytes(_elf(so_machine))
            assert "--require-hashes" in cmd and "--only-binary" in cmd
        elif "export" in cmd:
            assert "--no-dev" in cmd and "--frozen" in cmd
            Path(cmd[cmd.index("-o") + 1]).write_text("# locked closure\n")
        return ""

    return run


DEPS = ["finplan_contracts", "jsonschema", "rfc8785", "boto3"]


def test_bundle_targets_arm64_and_contains_the_closure(tmp_path):
    manifest = lambda_bundle.build_bundle(ROOT, tmp_path / "b", runner=fake_runner(DEPS, so_machine=lambda_bundle.ELF_AARCH64))
    assert manifest["python_platform"] == "aarch64-manylinux_2_28" == lambda_bundle.LAMBDA_PLATFORM
    assert (tmp_path / "b" / "finplan_tools" / "handler.py").is_file()
    assert not (tmp_path / "b" / "finplan_tools_testing").exists()
    cmd = lambda_bundle.install_commands(Path("r.txt"), tmp_path, python_platform=lambda_bundle.LAMBDA_PLATFORM, python="python3")[0]
    assert cmd[cmd.index("--python-platform") + 1] == "aarch64-manylinux_2_28"


def test_bundle_refuses_x86_binaries_and_missing_dependencies(tmp_path):
    with pytest.raises(lambda_bundle.BundleError, match="not an arm64 binary"):
        lambda_bundle.build_bundle(ROOT, tmp_path / "x86", runner=fake_runner(DEPS, so_machine=0x3E))
    with pytest.raises(lambda_bundle.BundleError, match="finplan_contracts missing"):
        lambda_bundle.build_bundle(ROOT, tmp_path / "src-only", runner=fake_runner(["jsonschema", "rfc8785", "boto3"]))


def test_bundle_refuses_test_only_content(tmp_path):
    with pytest.raises(lambda_bundle.BundleError, match="test-only"):
        lambda_bundle.build_bundle(ROOT, tmp_path / "b", runner=fake_runner(DEPS + ["finplan_tools_testing"]))


def test_bundle_rechecks_the_contract_wheel_digest(tmp_path, monkeypatch):
    fake_root = tmp_path / "root"
    (fake_root / "vendor").mkdir(parents=True)
    (fake_root / "vendor" / "w.whl").write_bytes(b"tampered")
    (fake_root / "contracts-pin.json").write_text(json.dumps({"artifact": "vendor/w.whl", "sha256": "0" * 64}))
    with pytest.raises(lambda_bundle.BundleError, match="digest"):
        lambda_bundle.build_bundle(fake_root, tmp_path / "b", runner=fake_runner(DEPS))


# ===================================================================== build stage
def _stub_synth(out: Path, release_id: str) -> Path:
    out.mkdir(parents=True)
    (out / "manifest.json").write_text("{}")
    (out / "x.template.json").write_text(json.dumps({"release": release_id}))
    return out


def _stub_bundle(root: Path, out: Path) -> dict:
    out.mkdir(parents=True, exist_ok=True)
    return {"unzipped_bytes": 1, "files": 1, "python_platform": lambda_bundle.LAMBDA_PLATFORM}


@pytest.fixture
def isolated_root(tmp_path, monkeypatch):
    """A copy-free stand-in for the repository root (only what run_build reads)."""
    root = tmp_path / "repo"
    (root / "config").mkdir(parents=True)
    (root / "config" / "environments.json").write_text(json.dumps({"region": "us-east-2"}))
    (root / "contracts-pin.json").write_text((ROOT / "contracts-pin.json").read_text())
    return root


def test_build_produces_release_info(isolated_root, tmp_path):
    info = build_stage.run_build(isolated_root, tmp_path / "out", source_commit="a" * 40, run_tests=False, gates=(), synth_fn=_stub_synth, bundle_fn=_stub_bundle, log=lambda _m: None)
    doc = json.loads((tmp_path / "out" / "release-info.json").read_text())
    assert doc["release_id"] == info.release_id and doc["artifact_digest"].startswith("sha256:")
    assert doc["contract_version"] == json.loads((ROOT / "contracts-pin.json").read_text())["version"] and doc["served_contract_majors"]
    assert (tmp_path / "out" / "cdk.out" / "manifest.json").is_file()


def test_failing_gate_produces_no_artifact(isolated_root, tmp_path, monkeypatch):
    from scripts import build_gates

    monkeypatch.setattr(build_gates, "run_gates", lambda ctx, stage="all", only=None: [build_gates.GateResult("leak-scan", ["leak"])])
    with pytest.raises(build_stage.BuildFailed, match="no artifact"):
        build_stage.run_build(isolated_root, tmp_path / "out", source_commit="a" * 40, run_tests=False, synth_fn=_stub_synth, bundle_fn=_stub_bundle, log=lambda _m: None)
    assert not (tmp_path / "out").exists()
    with pytest.raises(build_stage.BuildFailed, match="commit"):
        build_stage.run_build(isolated_root, tmp_path / "out2", source_commit="main", run_tests=False, gates=(), synth_fn=_stub_synth, bundle_fn=_stub_bundle)


def test_source_only_synth_is_refused_by_the_build(isolated_root, tmp_path):
    from infra.stacks.lambda_code import SourceOnlyCodeError

    def refusing(out: Path, release_id: str) -> Path:
        raise SourceOnlyCodeError("no bundle")

    with pytest.raises(build_stage.BuildFailed, match="source-only"):
        build_stage.run_build(isolated_root, tmp_path / "out", source_commit="a" * 40, run_tests=False, gates=(), synth_fn=refusing, bundle_fn=_stub_bundle, log=lambda _m: None)


def test_offline_suites_fail_on_zero_tests(tmp_path):
    def run(cmd, cwd, env):
        junit = Path(next(a for a in cmd if a.startswith("--junitxml=")).split("=", 1)[1])
        junit.write_text('<testsuite tests="0" skipped="0" failures="0" errors="0"/>')
        assert "FINPLAN_TARGET_ENV" not in env
        return SimpleNamespace(returncode=5)

    with pytest.raises(build_stage.BuildFailed):
        build_stage.run_offline_suites(tmp_path, run=run, log=lambda _m: None)


def test_build_stage_s3_access_uses_the_sigv4_helper():
    """Lesson L4: every script reaches S3 only through finplan_tools.core.aws_clients.s3_client."""
    import re

    bare = re.compile(r"""\.client\(\s*["']s3["']""")
    for p in sorted((ROOT / "scripts").glob("*.py")):
        if p.name == "build_gates.py":
            continue
        assert not bare.search(p.read_text()), p.name
    assert "s3_client(" in (ROOT / "scripts" / "build_stage.py").read_text() and "s3_client(" in (ROOT / "scripts" / "stage_runner.py").read_text()
