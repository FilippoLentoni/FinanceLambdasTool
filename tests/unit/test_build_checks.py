"""Contract pin (task 1.2), build-stage scans (task 1.3), artifact check (task 3.3), registry and handler.

Negative scan inputs are generated at test time under ``tmp_path`` (never committed): this repository
is public and its own leak and copied-``$id`` scans must stay clean.
"""

from __future__ import annotations

import json
import shutil
import zipfile
from pathlib import Path

import pytest

from scripts import build_gates
from scripts.check_artifact import artifact_problems
from scripts.check_contracts_pin import check, find_source_wheel, repin

ROOT = Path(__file__).resolve().parents[2]


# ================================================================ contracts pin (CS-04 consumer)
def test_pin_passes():
    assert check(ROOT) == []


def _copy_pin_tree(tmp_path: Path) -> Path:
    for name in ("contracts-pin.json", "pyproject.toml", "uv.lock"):
        shutil.copy(ROOT / name, tmp_path / name)
    shutil.copytree(ROOT / "vendor", tmp_path / "vendor")
    return tmp_path


def test_digest_mismatch_fails(tmp_path):
    root = _copy_pin_tree(tmp_path)
    pin = json.loads((root / "contracts-pin.json").read_text())
    pin["sha256"] = "0" * 64
    (root / "contracts-pin.json").write_text(json.dumps(pin))
    problems = check(root, check_installed=False)
    assert any("Digest mismatch" in p for p in problems)


def test_tampered_wheel_fails(tmp_path):
    root = _copy_pin_tree(tmp_path)
    wheel = root / json.loads((root / "contracts-pin.json").read_text())["artifact"]
    wheel.write_bytes(wheel.read_bytes() + b"x")
    assert any("Digest mismatch" in p for p in check(root, check_installed=False))


def test_range_pin_fails(tmp_path):
    root = _copy_pin_tree(tmp_path)
    py = root / "pyproject.toml"
    version = json.loads((root / "contracts-pin.json").read_text())["version"]
    text = py.read_text()
    assert f'"finplan-contracts=={version}"' in text
    py.write_text(text.replace(f'"finplan-contracts=={version}"', '"finplan-contracts>=1.0"'))
    assert any("exactly" in p for p in check(root, check_installed=False))


def test_zero_x_pin_is_beta_only(tmp_path):
    root = _copy_pin_tree(tmp_path)
    pin = json.loads((root / "contracts-pin.json").read_text())
    pin["version"] = "0.2.2"
    (root / "contracts-pin.json").write_text(json.dumps(pin))
    assert any("beta only" in p for p in check(root, env="prod", check_installed=False))


def test_repin_is_one_command(tmp_path):
    root = _copy_pin_tree(tmp_path)
    src = tmp_path / "upstream"
    src.mkdir()
    current = root / json.loads((root / "contracts-pin.json").read_text())["artifact"]
    newer = src / "finplan_contracts-1.1.0-py3-none-any.whl"
    newer.write_bytes(current.read_bytes() + b"\0")
    (src / "finplan_contracts-0.9.0-py3-none-any.whl").write_bytes(b"old")
    assert find_source_wheel(src) == newer
    version, digest = repin(root, src, run_lock=False)
    pin = json.loads((root / "contracts-pin.json").read_text())
    assert version == "1.1.0" and pin["version"] == "1.1.0" and pin["sha256"] == digest
    assert pin["artifact"] == "vendor/finplan-contracts/finplan_contracts-1.1.0-py3-none-any.whl"
    assert [p.name for p in (root / "vendor" / "finplan-contracts").glob("*.whl")] == [newer.name]
    text = (root / "pyproject.toml").read_text()
    assert '"finplan-contracts==1.1.0"' in text and "finplan_contracts-1.1.0-py3-none-any.whl" in text


# ================================================================ build-stage scans (1.3)
def test_pre_gates_pass_on_repository():
    results = build_gates.run_gates(build_gates.GateContext(), "pre")
    failing = {r.name: r.problems for r in results if not r.ok}
    assert failing == {}


def _ctx(root: Path) -> build_gates.GateContext:
    return build_gates.GateContext(root=root)


def test_copied_id_negative(tmp_path):
    """CS-01: a copied contract schema anywhere in the repo fails the build."""
    from finplan_contracts.schemas import contracts_root

    src = contracts_root() / "core" / "v1" / "caller.json"
    (tmp_path / "schemas").mkdir()
    shutil.copy(src, tmp_path / "schemas" / "caller.json")
    assert build_gates.gate_copied_id(_ctx(tmp_path))


def test_leak_scan_negative(tmp_path):
    """OWN-03: an account ID or an account-bearing ARN in a repo file fails the build."""
    digits = "".join(str((i * 7 + 3) % 10) for i in range(12))
    (tmp_path / "notes.md").write_text(f"role arn:aws:iam::{digits}:role/some-role\n")
    assert build_gates.gate_leak_scan(_ctx(tmp_path))


def test_live_permission_negative(tmp_path):
    """ENV-05: a policy granting a live trading/payment action fails the build."""
    doc = {"Statement": [{"Effect": "Allow", "Action": ["payments:CreatePayment"], "Resource": "*"}]}
    (tmp_path / "policy.json").write_text(json.dumps(doc))
    assert build_gates.gate_live_perm_scan(_ctx(tmp_path))


def test_tool_inventory_deny_list():
    from finplan_tools.core.registry import inventory_problems, register_tool

    assert inventory_problems(["get_plan", "execute_trade", "place_order", "wallet_send", "record_execution"]) == [
        "tool 'execute_trade' names a denied capability (execute, trade)",
        "tool 'place_order' names a denied capability (order)",
        "tool 'wallet_send' names a denied capability (wallet)",
        "tool 'record_execution' names a denied capability (execution)",
    ]
    with pytest.raises(ValueError):
        register_tool("execute_plan", description="x")
    with pytest.raises(ValueError):
        register_tool("not_in_catalog", description="x")


def test_config_gate_negative(tmp_path):
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "tool-limits.default.json").write_text(json.dumps({"response_max_bytes": 65536}))
    envs = json.loads((ROOT / "config" / "environments.json").read_text())
    envs["environments"]["prod"]["direct_test_write_tools"] = True
    envs["environments"]["beta"]["log_retention_days"] = 7
    (tmp_path / "config" / "environments.json").write_text(json.dumps(envs))
    problems = build_gates.gate_config(_ctx(tmp_path))
    assert any("differs" in p for p in problems) and any("prod" in p for p in problems) and any("log_retention_days" in p for p in problems)


# ================================================================ artifact check (3.3, ENVW-05)
def _bundle(tmp_path: Path, extra: dict[str, str]) -> Path:
    b = tmp_path / "bundle"
    shutil.copytree(ROOT / "src" / "finplan_tools", b / "finplan_tools", ignore=shutil.ignore_patterns("__pycache__"))
    for rel, text in extra.items():
        p = b / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    return b


def test_clean_bundle_passes(tmp_path):
    assert artifact_problems(_bundle(tmp_path, {"boto3/__init__.py": ""})) == []


@pytest.mark.parametrize("extra", [
    {"finplan_tools_testing/mock_platform.py": "class MockPlatform: ..."},
    {"finplan_tools/backends/mock_jobs.py": "x = 1"},
    {"finplan_tools/tools/helper.py": "from finplan_tools_testing.runtime import offline_runtime"},
    {"finplan_tools/tools/x.py": "class MockProducer: ..."},
    {"yfinance/__init__.py": ""},
    {"exchange_calendars-4.13.2.dist-info/METADATA": ""},
    {"finplan_tools/tools/q.py": "import yfinance as yf"},
    {"tests/test_x.py": ""},
])
def test_bundle_with_mock_or_provider_fails(tmp_path, extra):
    assert artifact_problems(_bundle(tmp_path, extra))


def test_zip_bundle_and_missing_package(tmp_path):
    b = _bundle(tmp_path, {"finplan_tools_testing/__init__.py": ""})
    z = tmp_path / "bundle.zip"
    with zipfile.ZipFile(z, "w") as zf:
        for p in b.rglob("*"):
            if p.is_file():
                zf.write(p, p.relative_to(b).as_posix())
    assert any("test-only" in p for p in artifact_problems(z))
    empty = tmp_path / "empty"
    empty.mkdir()
    assert artifact_problems(empty) == ["the artifact does not contain the finplan_tools package"]


def test_wheel_excludes_testing_package():
    import tomllib

    cfg = tomllib.loads((ROOT / "pyproject.toml").read_text())
    assert cfg["tool"]["hatch"]["build"]["targets"]["wheel"]["packages"] == ["src/finplan_tools"]


# ================================================================ registry and handler
def test_catalog_matches_pinned_schemas():
    from finplan_tools.core.contracts import store
    from finplan_tools.core.registry import CATALOG

    assert len(CATALOG) == 38
    for e in CATALOG.values():
        assert e.input_schema in store() and e.output_schema in store()
        assert e.prod_direct_test is (not e.state_changing)
        assert e.role_class == ("reader" if not e.state_changing else e.role_class)
    tools_dir = {p.name.removesuffix("-request.json").replace("-", "_") for ns in ("core", "finance") for p in (store().root / ns / "v1" / "tools").glob("*-request.json") if p.with_name(p.name.replace("-request.json", "-response.json")).exists()}
    assert tools_dir == set(CATALOG)


def test_handler_for_unregistered_tool_returns_envelope(offline):
    from finplan_tools.core import registry
    from finplan_tools.handler import invoke
    from finplan_tools.tools import load_all

    load_all()  # every tool module registered first, then one registration removed
    saved = registry._REGISTRY.pop("get_plan", None)  # noqa: SLF001
    try:
        resp = invoke("get_plan", offline.event({"plan_id": "pl_01KM0000000000000000000001"}), None, offline.runtime)
        assert resp["code"] == "INTERNAL" and resp["details"]["reason"] == "tool_not_registered"
    finally:
        if saved is not None:
            registry._REGISTRY["get_plan"] = saved  # noqa: SLF001


def test_register_tool_and_invoke_through_handler(offline):
    from finplan_tools.core import registry
    from finplan_tools.handler import invoke

    saved = registry._REGISTRY.get("get_plan_version")  # noqa: SLF001

    @registry.register_tool("get_plan_version", description="test registration", replace=True)
    def _t(ctx, req):
        return ctx.platform.get_plan_version(req["plan_version_id"], ctx.meta)

    try:
        pf = offline.platform.add_portfolio()
        _, pv = offline.platform.add_plan(pf)
        resp = invoke("get_plan_version", offline.event({"plan_version_id": pv}), None, offline.runtime)
        assert resp["plan_version"]["plan_version_id"] == pv
        spec = registry.get_tool("get_plan_version")
        assert spec.input_schema_id.endswith("/core/v1/tools/get-plan-version-request.json")
    finally:
        if saved is not None:
            registry._REGISTRY["get_plan_version"] = saved  # noqa: SLF001
        else:
            registry._unregister("get_plan_version")  # noqa: SLF001


def test_lambda_handler_misconfigured_returns_envelope(monkeypatch):
    import finplan_tools.handler as h

    monkeypatch.setattr(h, "_RUNTIME", None)
    monkeypatch.delenv("FINPLAN_ENV", raising=False)
    resp = h.handler({}, None)
    assert resp["code"] == "INTERNAL" and resp["details"]["reason"] == "runtime_unavailable"


def test_lambda_handler_dispatches_configured_tool(monkeypatch, offline):
    import finplan_tools.handler as h

    rt = offline.runtime
    monkeypatch.setattr(h, "_RUNTIME", rt)
    monkeypatch.setattr(rt, "settings", type(rt.settings)(environment="beta", release_id=None, tool_name=None))
    assert h.handler({}, None)["details"]["reason"] == "tool_not_configured"
    monkeypatch.setattr(rt, "settings", type(rt.settings)(environment="beta", release_id=None, tool_name="get_plan"))
    resp = h.handler({"arguments": {}}, None)  # no invocation marker
    assert resp["code"] in ("UNAUTHORIZED", "INTERNAL")


def test_offline_env_never_applies_to_deployed_suites():
    from tests.offline_env import apply_offline_environment, deployed_suite_mode

    env = {"FINPLAN_TARGET_ENV": "beta", "AWS_PROFILE": "x"}
    assert deployed_suite_mode(env)
    env2 = {"AWS_PROFILE": "x", "AWS_SESSION_TOKEN": "t"}
    apply_offline_environment(env2)
    assert "AWS_PROFILE" not in env2 and "AWS_SESSION_TOKEN" not in env2 and env2["AWS_EC2_METADATA_DISABLED"] == "true"
