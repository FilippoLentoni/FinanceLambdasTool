#!/usr/bin/env python3
"""One-time authenticated bootstrap of the FinanceLambdasTool pipeline (task 9.6; REL-09, REL-10;
contracts D6, D11, D12; ENV-12, ENV-13). Runbook: ``docs/bootstrap.md``.

DO NOT RUN during implementation work. The user approved the bootstrap in principle on 2026-10-07;
it runs once, by a human, only after this IaC is synthesized, and only after the exact stacks and a
cost estimate have been shown and confirmed.

The sequence is the contract package's :func:`finplan_contracts.bootstrap.run_bootstrap` (never
re-implemented); this entry point supplies the FinanceLambdasTool specifics:

1. **Assembly** (:func:`bootstrap_assembly`): refuses without a synthesized assembly, copies only the
   two account-level stacks ``finplan-shared-financelambdastool-pipeline-store`` and
   ``finplan-shared-financelambdastool-tooling`` into ``cdk.out.bootstrap/`` and refuses an assembly
   that references the CDK bootstrap (``cdk-hnb659fds`` roles, ``cdk-*-assets`` buckets) or carries
   container-image assets (lesson L1).
2. **Pre-run plan** (contract): the exact stacks with their resource types and a monthly estimate from
   the AWS Price List API (no price is written in this repository). The three environment stacks are
   NOT created by the bootstrap; the pipeline deploys them later (listed for information).
3. **Caller, region, connection, scoped-role checks** (contract). A root caller is accepted (with the
   scoped/MFA recommendation). The CodeConnection is the **same existing connection the platform
   uses**: when the local configuration names none, it is read (read-only) from
   ``/finplan/shared/financialplanning/config/codeconnection-ref`` (lesson L6).
4. **Confirmation**: the operator types ``deploy``. Nothing is written before it.
5. **Connection reference** ``/finplan/shared/financelambdastool/config/codeconnection-ref`` (contract).
6. **Deploy** (:class:`ToolingDeployer`): ``npx aws-cdk@2 deploy`` of the filtered assembly, then:

   - the 30-day CodeBuild log groups (:func:`apply_codebuild_log_retention`; contract gap
     ``pipeline-logs``, lesson L6), before any build can run;
   - default ``/finplan/<env>/financelambdastool/config/tool-limits`` per environment, **unless
     present** (:func:`write_tool_limits`);
   - each environment's ``/finplan/<env>/financelambdastool/config/direct-test-principal-name`` from
     the local untracked configuration (:func:`write_direct_test_principals`): a principal NAME only,
     never an ARN; the account root is refused; when no name is configured nothing is written and
     no direct-test grant exists (fail closed);
   - ``/finplan/shared/financelambdastool/config/budget-enforced-role-names`` (the account-level
     pipeline and build roles, for the FinancialPlanning budget action).

   No budget, no boundary and no CodeConnection is created: they are FinancialPlanning-owned or existing.
7. **Source-stage dry run** (contract): an execution must fetch ``main`` of
   ``FilippoLentoni/FinanceLambdasTool`` before the stages after Source are enabled; otherwise it
   stops with the extend-the-GitHub-App-installation message and the deploy stages stay disabled.

Local configuration (:func:`read_local_config`) is read **the same way as the platform's and
FinanceModel's**: ``--config PATH``, else ``$FINPLAN_BOOTSTRAP_CONFIG``, else the shared
``~/.finplan/bootstrap.json`` (account, primary region, the existing connection), refused inside the
repository; then the optional overlay ``~/.finplan/financelambdastool-bootstrap.json`` (for example
``direct_test_principal_name`` or a per-environment map ``direct_test_principal_names``); then
``FINPLAN_ACCOUNT_ID`` / ``FINPLAN_PRIMARY_REGION`` / ``FINPLAN_CODECONNECTION_ARN``. Platform-only keys
of the shared file are ignored and never printed.

Credentials come from the default boto3 chain only. Every AWS client is injected, so the unit suite
runs the whole sequence with mocks and no AWS call.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
for _p in (ROOT, ROOT / "src"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from finplan_contracts import bootstrap as contract_bootstrap
from finplan_contracts import budget as contract_budget
from finplan_contracts import ssm as contract_ssm
from finplan_contracts.bootstrap import BootstrapConfig, BootstrapStop, Clients, Plan

from finplan_tools.core.config import DEFAULT_TOOL_LIMITS, ENVIRONMENTS
from infra.stacks import naming as n
from infra.stacks.tooling import STORE_STACK_NAME, TOOLING_STACK_NAME

__all__ = [
    "BOOTSTRAP_STACKS",
    "PLATFORM_CONNECTION_PARAMETER",
    "ToolingDeployer",
    "apply_codebuild_log_retention",
    "bootstrap_assembly",
    "cdk_bootstrap_references",
    "direct_test_names",
    "load_bootstrap_config",
    "main",
    "read_local_config",
    "run",
    "shared_enforced_role_names",
    "write_direct_test_principals",
    "write_tool_limits",
]

REPO = n.REPO
GITHUB_REPOSITORY = "FilippoLentoni/FinanceLambdasTool"
BOOTSTRAP_STACKS = (STORE_STACK_NAME, TOOLING_STACK_NAME)
DEFAULT_ASSEMBLY = ROOT / "cdk.out"
BOOTSTRAP_ASSEMBLY = ROOT / "cdk.out.bootstrap"
#: Optional FinanceLambdasTool overlay on top of the shared configuration (never required).
DEFAULT_CONFIG = Path("~/.finplan/financelambdastool-bootstrap.json")
SHARED_CONFIG = contract_bootstrap.DEFAULT_CONFIG_PATH
#: Keys of the shared file that describe the platform's own pipeline, or are platform-only.
_PLATFORM_ONLY_KEYS = ("repo", "github_repository", "pipeline_name", "source_stage", "next_stage", "budget_notification_email", "scope_budget_to_project_tag")
DRY_RUN_RECORD = Path("~/.finplan/financelambdastool-source-dry-run.json")
PLATFORM_CONNECTION_PARAMETER = contract_ssm.build(contract_ssm.SHARED, "financialplanning", "config", "codeconnection-ref")
_CDK_BOOTSTRAP_RE = re.compile(r"cdk-hnb659fds|cdk-[a-z0-9]+-assets-")


# ===================================================================== assembly
def cdk_bootstrap_references(path: Path) -> list[str]:
    """Files of an assembly directory that reference CDK bootstrap roles or asset buckets."""
    return [p.relative_to(path).as_posix() for p in sorted(path.rglob("*.json")) if _CDK_BOOTSTRAP_RE.search(p.read_text(encoding="utf-8", errors="replace"))]


def bootstrap_assembly(assembly: str | os.PathLike[str], out: str | os.PathLike[str]) -> Path:
    """Copy only the two account-level stacks (and their asset manifests) into ``out`` (lesson L1)."""
    src = Path(assembly)
    manifest_path = src / "manifest.json"
    if not manifest_path.is_file():
        raise BootstrapStop("prerun", f"the bootstrap IaC is not synthesized: no cloud assembly at {src} (run: uv run python scripts/synth.py). Nothing was deployed.")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    arts = manifest.get("artifacts") or {}
    keep = {aid: a for aid, a in arts.items() if a.get("type") == "aws:cloudformation:stack" and (a.get("properties") or {}).get("stackName") in BOOTSTRAP_STACKS}
    names = {(a.get("properties") or {}).get("stackName") for a in keep.values()}
    missing = [s for s in BOOTSTRAP_STACKS if s not in names]
    if missing:
        raise BootstrapStop("prerun", f"the synthesized assembly does not contain the tooling stacks {missing} (synthesize all three environments); nothing was deployed")
    for art in list(keep.values()):
        for dep in art.get("dependencies") or []:
            if dep in arts and arts[dep].get("type") == "cdk:asset-manifest":
                keep[dep] = arts[dep]
    dst = Path(out)
    shutil.rmtree(dst, ignore_errors=True)
    dst.mkdir(parents=True)
    files: set[str] = set()
    for art in keep.values():
        props = art.get("properties") or {}
        for key in ("templateFile", "file"):
            if props.get(key):
                files.add(props[key])
        if art.get("additionalMetadataFile"):
            files.add(art["additionalMetadataFile"])
        if art.get("type") == "cdk:asset-manifest":
            doc = json.loads((src / props["file"]).read_text(encoding="utf-8"))
            for asset in (doc.get("files") or {}).values():
                files.add(str((asset.get("source") or {}).get("path")))
            if doc.get("dockerImages"):
                raise BootstrapStop("prerun", "the tooling stacks must not contain container-image assets")
    for rel in sorted(files):
        s = src / rel
        if s.is_dir():
            shutil.copytree(s, dst / rel)
        elif s.is_file():
            shutil.copy2(s, dst / rel)
        else:
            raise BootstrapStop("prerun", f"{rel} is missing from the cloud assembly")
    out_manifest = {**{k: v for k, v in manifest.items() if k != "artifacts"}, "artifacts": {}}
    for aid, art in keep.items():
        art = json.loads(json.dumps(art))
        art["dependencies"] = [d for d in art.get("dependencies") or [] if d in keep]
        props = art.get("properties") or {}
        if "additionalDependencies" in props:
            props["additionalDependencies"] = [d for d in props["additionalDependencies"] if d in keep]
        out_manifest["artifacts"][aid] = art
    (dst / "manifest.json").write_text(json.dumps(out_manifest, indent=1) + "\n", encoding="utf-8")
    refs = cdk_bootstrap_references(dst)
    if refs:
        raise BootstrapStop("prerun", f"the bootstrap assembly references the CDK bootstrap stack (cdk-hnb659fds roles or cdk-*-assets buckets) in {refs}; it must deploy without CDKToolkit")
    return dst


# ===================================================================== configuration
def _not_found(exc: Exception) -> bool:
    code = getattr(exc, "response", {}).get("Error", {}).get("Code") if hasattr(exc, "response") else None
    return code == "ParameterNotFound" or type(exc).__name__ == "ParameterNotFound"


def _get(ssm: Any, name: str) -> str | None:
    try:
        return ssm.get_parameter(Name=name)["Parameter"]["Value"]
    except Exception as exc:
        if _not_found(exc):
            return None
        raise


def _inside(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


def read_local_config(path: str | os.PathLike[str] | None = None, *, environ: Mapping[str, str] | None = None, overlay: Path | None = DEFAULT_CONFIG, repo_root: Path = ROOT) -> dict[str, Any]:
    """The local untracked configuration, resolved like the platform's (module docstring)."""
    env = os.environ if environ is None else environ
    shared = Path(path) if path else Path(env[contract_bootstrap.CONFIG_ENV]) if env.get(contract_bootstrap.CONFIG_ENV) else SHARED_CONFIG
    data: dict[str, Any] = {}
    for candidate, drop in ((shared, _PLATFORM_ONLY_KEYS), (overlay, _PLATFORM_ONLY_KEYS[3:])):
        if candidate is None:
            continue
        candidate = Path(candidate).expanduser()
        if not candidate.is_file():
            continue
        if _inside(candidate, repo_root):
            raise BootstrapStop("configuration", f"bootstrap configuration {candidate} is inside the repository; keep it local and untracked (for example ~/.finplan/bootstrap.json)")
        doc = json.loads(candidate.read_text(encoding="utf-8"))
        data.update({k: v for k, v in doc.items() if k not in drop})
    for env_key, key in (("FINPLAN_ACCOUNT_ID", "account_id"), ("FINPLAN_PRIMARY_REGION", "primary_region"), ("FINPLAN_CODECONNECTION_ARN", "codeconnection_arn")):
        if env.get(env_key):
            data[key] = env[env_key]
    return data


def load_bootstrap_config(local: Mapping[str, Any], ssm: Any) -> BootstrapConfig:
    """The contract configuration for ``financelambdastool``; the CodeConnection defaults to the platform's (reused)."""
    data = {k: v for k, v in local.items() if k not in ("direct_test_principal_name", "direct_test_principal_names")}
    data["repo"] = REPO
    data["github_repository"] = GITHUB_REPOSITORY
    data["pipeline_name"] = n.PIPELINE_NAME
    for key in _PLATFORM_ONLY_KEYS[3:]:
        data.pop(key, None)
    if not data.get("codeconnection_arn"):
        reused = _get(ssm, PLATFORM_CONNECTION_PARAMETER)
        if not reused:
            raise BootstrapStop("connection", f"no CodeConnection in the local configuration and none published at {PLATFORM_CONNECTION_PARAMETER}; bootstrap FinancialPlanning first or name the existing connection locally")
        data["codeconnection_arn"] = reused
    try:
        return BootstrapConfig.from_mapping(data)
    except ValueError as exc:
        raise BootstrapStop("configuration", str(exc)) from None


def direct_test_names(local: Mapping[str, Any]) -> dict[str, str]:
    """``{env: validated name}`` from ``direct_test_principal_names`` (per environment) or the single
    ``direct_test_principal_name`` (the project owner, every environment). Root, ARNs, wildcards,
    account IDs and lists are refused (ENVW-08)."""
    from scripts.predeploy import normalize_direct_test_principal

    per_env = local.get("direct_test_principal_names")
    single = local.get("direct_test_principal_name")
    if per_env is not None and not isinstance(per_env, Mapping):
        raise BootstrapStop("configuration", "direct_test_principal_names must map an environment to one principal name")
    out: dict[str, str] = {}
    for env in ENVIRONMENTS:
        value = (per_env or {}).get(env, single)
        if value in (None, ""):
            continue
        if not isinstance(value, str):
            raise BootstrapStop("configuration", f"the {env} direct-test principal must be one name")
        try:
            normalize_direct_test_principal(value)
        except ValueError as exc:
            raise BootstrapStop("configuration", f"{env}: {exc}") from None
        out[env] = value.strip()
    return out


# ===================================================================== post-deploy writes
def _put(ssm: Any, name: str, value: str, *, overwrite: bool, description: str) -> None:
    decision = contract_ssm.check_write(name, contract_ssm.Writer(REPO, "bootstrap"))
    if not decision:
        raise BootstrapStop("ssm", "; ".join(decision.reasons))
    ssm.put_parameter(Name=name, Value=value, Type="String", Overwrite=overwrite, Description=description)


def write_tool_limits(ssm: Any, *, out: Callable[[str], None] = print) -> list[str]:
    """Default ``tool-limits`` per environment, unless present (the user's edits are kept)."""
    written = []
    body = json.dumps(DEFAULT_TOOL_LIMITS, sort_keys=True, separators=(",", ":"))
    for env in ENVIRONMENTS:
        name = n.own_ssm(env, "config", "tool-limits")
        if _get(ssm, name) is not None:
            out(f"[OK] {name} present; kept")
            continue
        _put(ssm, name, body, overwrite=False, description="FinanceLambdasTool tool limits (D5, D7); written once by the bootstrap")
        written.append(name)
        out(f"[OK] wrote default {name}")
    return written


def write_direct_test_principals(ssm: Any, names: Mapping[str, str], *, out: Callable[[str], None] = print) -> list[str]:
    """Each environment's direct-test principal NAME (never an ARN); none configured -> nothing written."""
    written = []
    for env, value in sorted(names.items()):
        name = n.own_ssm(env, "config", "direct-test-principal-name")
        _put(ssm, name, value, overwrite=True, description="Name of the single direct-test principal (the project owner); never an ARN")
        written.append(name)
        out(f"[OK] wrote {name} (value not printed)")
    for env in ENVIRONMENTS:
        if env not in names:
            out(f"[INFO] no direct-test principal configured for {env}: no direct-test invoke grant (fail closed)")
    return written


def shared_enforced_role_names() -> list[str]:
    """Account-level tooling roles the platform's budget action denies at 100%."""
    return [n.shared_name("pipeline", "role"), n.shared_name("pipeline-build-project", "role")]


def apply_codebuild_log_retention(logs: Any, project_names: list[str], *, days: int = n.LOG_RETENTION_DAYS, out: Callable[[str], None] = print) -> list[str]:
    """Create ``/aws/codebuild/<project>`` with the cost tags (if absent) and set ``days`` retention
    (lesson L6; contract gap ``pipeline-logs``). Idempotent."""
    tags = {**contract_ssm.cost_allocation_tags(REPO, contract_ssm.SHARED, "pipeline-build-project")}
    tags.pop("run-id", None)
    done = []
    for project in project_names:
        group = f"/aws/codebuild/{project}"
        try:
            logs.create_log_group(logGroupName=group, tags=tags)
        except Exception as exc:
            code = getattr(exc, "response", {}).get("Error", {}).get("Code") if hasattr(exc, "response") else type(exc).__name__
            if code != "ResourceAlreadyExistsException":
                raise
        logs.put_retention_policy(logGroupName=group, retentionInDays=days)
        done.append(group)
        out(f"[OK] {group}: {days}-day retention")
    return done


# ===================================================================== deploy
class ToolingDeployer:
    """The ``deployer`` callback of ``run_bootstrap`` (runs only after the operator's confirmation)."""

    def __init__(self, assembly: Path, ssm: Any, logs: Any, *, region: str, dry_run_passed: bool, direct_test: Mapping[str, str], runner: Callable[..., Any] = subprocess.run, out: Callable[[str], None] = print) -> None:
        self.assembly = assembly
        self.ssm = ssm
        self.logs = logs
        self.region = region
        self.dry_run_passed = dry_run_passed
        self.direct_test = dict(direct_test)
        self.runner = runner
        self.out = out
        self.commands: list[list[str]] = []

    def command(self) -> list[str]:
        return ["npx", "--yes", "aws-cdk@2", "deploy", "--app", str(self.assembly), "--all", "--require-approval", "never", "--progress", "events", "--parameters", f"{TOOLING_STACK_NAME}:SourceDryRunPassed={'true' if self.dry_run_passed else 'false'}"]

    def __call__(self, stack_names: list[str]) -> None:
        from infra.stacks.pipeline import codebuild_project_names

        if sorted(stack_names) != sorted(BOOTSTRAP_STACKS):
            raise BootstrapStop("deploy", f"the bootstrap deploys only {list(BOOTSTRAP_STACKS)}, got {stack_names}")
        allocation = _get(self.ssm, contract_budget.ALLOCATION_PARAMETER)
        if allocation is None:
            self.out(f"[WARN] {contract_budget.ALLOCATION_PARAMETER} is absent: the pre-deploy limit check bounds against the contract defaults until the FinancialPlanning bootstrap writes it (FinanceLambdasTool never writes it)")
        else:
            self.out("[OK] shared budget allocation present (FinancialPlanning-owned, read-only)")
        cmd = self.command()
        self.commands.append(cmd)
        self.out("running: " + " ".join(cmd))
        env = {**os.environ, "AWS_REGION": self.region, "AWS_DEFAULT_REGION": self.region, "CDK_DISABLE_VERSION_CHECK": "1"}
        proc = self.runner(cmd, cwd=str(ROOT), env=env)
        if int(getattr(proc, "returncode", 1)) != 0:
            raise BootstrapStop("deploy", "cdk deploy of the FinanceLambdasTool tooling stacks failed (CloudFormation rolls back automatically)")
        apply_codebuild_log_retention(self.logs, codebuild_project_names(), out=self.out)
        write_tool_limits(self.ssm, out=self.out)
        write_direct_test_principals(self.ssm, self.direct_test, out=self.out)
        name = contract_ssm.build(contract_ssm.SHARED, REPO, "config", "budget-enforced-role-names")
        _put(self.ssm, name, ",".join(shared_enforced_role_names()), overwrite=True, description="FinanceLambdasTool account-level roles the budget action denies at 100%")
        self.out(f"[OK] wrote {name}")


# ===================================================================== orchestration
def _interactive_approve(plan: Plan) -> bool:  # pragma: no cover - interactive
    print("\nThe stacks and cost estimate above will be deployed under the user's in-principle approval of 2026-10-07.")
    print(f"Later, the pipeline (not this bootstrap) deploys {', '.join(n.stack_name(e) for e in ENVIRONMENTS)}.")
    return input("Type 'deploy' to deploy exactly these stacks, anything else to stop: ").strip() == "deploy"


def run(
    config: BootstrapConfig,
    clients: Clients,
    *,
    logs: Any,
    session_region: str | None,
    direct_test: Mapping[str, str] | None = None,
    assembly: Path = DEFAULT_ASSEMBLY,
    bootstrap_dir: Path = BOOTSTRAP_ASSEMBLY,
    approve: Callable[[Plan], bool] = _interactive_approve,
    runner: Callable[..., Any] = subprocess.run,
    record_path: Path | None = None,
    sleep: Callable[[float], None] | None = None,
    usage_rules: Mapping[str, Any] | None = None,
    out: Callable[[str], None] = print,
) -> contract_bootstrap.Report:
    filtered = bootstrap_assembly(assembly, bootstrap_dir)
    record = Path(record_path).expanduser() if record_path else None
    passed = bool(record and record.is_file() and json.loads(record.read_text(encoding="utf-8")).get("deploy_stages_enabled") is True)
    deployer = ToolingDeployer(filtered, clients.ssm, logs, region=config.primary_region, dry_run_passed=passed, direct_test=direct_test or {}, runner=runner, out=out)
    kwargs: dict[str, Any] = {}
    if sleep is not None:
        kwargs["sleep"] = sleep
    return contract_bootstrap.run_bootstrap(config, clients, session_region=session_region, assembly_dir=filtered, approve=approve, deployer=deployer, out=out, record_path=record, usage_rules=usage_rules, **kwargs)


def make_clients(region: str) -> tuple[Clients, Any]:
    """boto3 clients from the default credential chain (instance role, environment, profile)."""
    import boto3

    session = boto3.session.Session(region_name=region)
    clients = Clients(sts=session.client("sts"), codeconnections=session.client("codeconnections"), ssm=session.client("ssm"), codepipeline=session.client("codepipeline"), pricing=session.client("pricing", region_name="us-east-1"))
    return clients, session.client("logs")


def main(argv: list[str] | None = None) -> int:  # pragma: no cover - the authenticated run is a human step
    ap = argparse.ArgumentParser(description="One-time authenticated bootstrap of the FinanceLambdasTool pipeline. Read docs/bootstrap.md first.")
    ap.add_argument("--config", type=Path, default=None, help="local untracked configuration (default: $FINPLAN_BOOTSTRAP_CONFIG, else ~/.finplan/bootstrap.json, plus the optional ~/.finplan/financelambdastool-bootstrap.json overlay)")
    ap.add_argument("--assembly", type=Path, default=DEFAULT_ASSEMBLY)
    ap.add_argument("--record", type=Path, default=DRY_RUN_RECORD)
    args = ap.parse_args(argv)
    try:
        local = read_local_config(args.config)
        direct_test = direct_test_names(local)
    except (BootstrapStop, ValueError, OSError) as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 2
    region = str(local.get("primary_region") or "us-east-2")
    clients, logs = make_clients(region)
    args.record.expanduser().parent.mkdir(parents=True, exist_ok=True)
    try:
        config = load_bootstrap_config(local, clients.ssm)
        run(config, clients, logs=logs, session_region=region, direct_test=direct_test, assembly=args.assembly, record_path=args.record)
    except BootstrapStop as stop:
        print(f"[STOPPED] {stop.step}: {stop.message}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
