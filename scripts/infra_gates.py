#!/usr/bin/env python3
"""Post-synth build gates of the infrastructure task groups (8 and 9), appended to
:data:`scripts.build_gates.GATES`. Each takes the gate context (``ctx.assembly`` = the synthesized
cloud assembly) and returns a list of problems. All run offline.

====================  ==========================================================================
gate                  what
====================  ==========================================================================
ownership             every resource maps to a FinanceLambdasTool row of the pinned ownership
                      matrix (OWN-01; CDK helpers attributed, never skipped)
boundaries            every IAM role carries its environment's permission boundary (ENVW-02,
                      ENV-18) and shared-tagged resources hold no environment data (ENV-16)
live-perm-synth       no live-financial permission in any synthesized template (ENV-05)
pipeline-structure    contract pipeline standard (REL-05, ENV-09) and scoped deploy roles
cost                  no always-on or provisioned resource; cost tags everywhere (ENVW-07)
lambda-bundle         the code asset is a complete arm64 bundle when present (lesson L3)
log-retention         every log group is 30 days; every function and CodeBuild project has one (L6)
invoke-grants         Lambda grants only on the alias, only to the stage role, the direct-test
                      principal or the Gateway role; prod write tools never to the stage role or
                      the direct-test principal; no wildcard principal (ENVW-03, ENVW-04, ENVW-08)
env-wiring            an environment's template names only its own SSM paths and resources
                      (ENVW-01) and no literal endpoint, ARN or account
cdk-bootstrap-free    no ``cdk-hnb659fds`` role, ``cdk-*-assets`` bucket or bootstrap-version rule
                      anywhere in the assembly (lesson L1)
====================  ==========================================================================
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

__all__ = ["INFRA_GATES", "cost_problems", "grant_problems", "log_retention_problems", "templates_of", "wiring_problems"]

REPO = "financelambdastool"
ENVIRONMENTS = ("beta", "gamma", "prod")
_CDK_BOOTSTRAP = re.compile(r"cdk-hnb659fds|cdk-[a-z0-9]+-assets-|BootstrapVersion")
_LITERAL = re.compile(r"\b\d{12}\b|https://[a-z0-9]{10}\.execute-api\.")


def templates_of(assembly: Path) -> list[Path]:
    return sorted(assembly.rglob("*.template.json"))


def _load(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _need_assembly(ctx: Any) -> Path:
    if ctx.assembly is None or not (Path(ctx.assembly) / "manifest.json").is_file():
        raise FileNotFoundError("post-synth gates need a synthesized cloud assembly (--assembly)")
    return Path(ctx.assembly)


def _env_of(template: dict[str, Any]) -> str | None:
    for res in (template.get("Resources") or {}).values():
        for t in (res.get("Properties") or {}).get("Tags") or []:
            if isinstance(t, dict) and t.get("Key") == "environment":
                return str(t.get("Value"))
    return None


def _resources(template: dict[str, Any], rtype: str) -> dict[str, dict[str, Any]]:
    return {lid: r for lid, r in (template.get("Resources") or {}).items() if isinstance(r, dict) and r.get("Type") == rtype}


def _refs(node: Any) -> set[str]:
    out: set[str] = set()
    if isinstance(node, dict):
        for k, v in node.items():
            if k == "Ref" and isinstance(v, str):
                out.add(v)
            elif k == "Fn::GetAtt":
                out.add(v[0] if isinstance(v, list) else str(v).split(".")[0])
            else:
                out |= _refs(v)
    elif isinstance(node, list):
        for i in node:
            out |= _refs(i)
    return out


# ===================================================================== gates
def gate_ownership(ctx: Any) -> list[str]:
    from finplan_contracts.ownership import check_template

    problems: list[str] = []
    templates = templates_of(_need_assembly(ctx))
    for path in templates:
        report = check_template(_load(path), REPO, name=path.name).to_dict()
        problems += [f"ownership {path.name}: {p['logical_id']} ({p['resource_type']}): {p['message']}" for p in report["problems"]]
    ctx.note("ownership", f"{len(templates)} templates checked")
    return problems


def gate_boundaries(ctx: Any) -> list[str]:
    from finplan_contracts.boundaries import check_role_boundaries, check_shared_resources

    problems: list[str] = []
    for path in templates_of(_need_assembly(ctx)):
        t = _load(path)
        problems += [f"boundary {path.name}: {f}" for f in check_role_boundaries(t)]
        problems += [f"shared {path.name}: {f}" for f in check_shared_resources(t, repo=REPO)]
    return problems


def gate_live_perms_synth(ctx: Any) -> list[str]:
    from finplan_contracts import live_perms

    n, findings = live_perms.scan_paths(templates_of(_need_assembly(ctx)))
    ctx.note("live-perm-synth", f"{n} templates scanned")
    return [f"live-financial permission: {f}" for f in findings]


def gate_pipeline_structure(ctx: Any) -> list[str]:
    from finplan_contracts.bootstrap import check_deploy_roles
    from finplan_contracts.pipeline_check import check_pipeline_template

    problems: list[str] = []
    pipelines = 0
    for path in templates_of(_need_assembly(ctx)):
        t = _load(path)
        if not _resources(t, "AWS::CodePipeline::Pipeline"):
            continue
        pipelines += 1
        problems += [f"pipeline {path.name}: {f}" for f in check_pipeline_template(t)]
        problems += [f"deploy roles {path.name}: {p}" for p in check_deploy_roles(t)]
    if pipelines == 0:
        problems.append("no pipeline template in the assembly (synthesize all three environments)")
    return problems


ALWAYS_ON_TYPES = frozenset(
    {
        "AWS::EC2::Instance",
        "AWS::EC2::NatGateway",
        "AWS::EC2::EIP",
        "AWS::EC2::VPC",
        "AWS::ElasticLoadBalancingV2::LoadBalancer",
        "AWS::ElasticLoadBalancing::LoadBalancer",
        "AWS::SageMaker::Endpoint",
        "AWS::SageMaker::NotebookInstance",
        "AWS::RDS::DBInstance",
        "AWS::RDS::DBCluster",
        "AWS::ECS::Service",
        "AWS::EKS::Cluster",
        "AWS::ElastiCache::CacheCluster",
        "AWS::OpenSearchService::Domain",
        "AWS::Lambda::EventSourceMapping",
    }
)
_TAG_KEYS = ("project", "owner-repo", "environment", "logical-role")
#: Parent-attributed helpers (an own ``logical-role`` would bypass that attribution).
_PARENT_ATTRIBUTED = frozenset({"AWS::Logs::LogGroup"})


def cost_problems(template: dict[str, Any], name: str) -> list[str]:
    """ENVW-07: on-demand only, contract cost tags on every taggable resource."""
    problems: list[str] = []
    for lid, res in sorted((template.get("Resources") or {}).items()):
        if not isinstance(res, dict):
            continue
        rtype, props = str(res.get("Type")), res.get("Properties") or {}
        if rtype in ALWAYS_ON_TYPES:
            problems.append(f"{name}: {lid} ({rtype}) is always-on or networked compute")
        if rtype == "AWS::EC2::VPCEndpoint":
            problems.append(f"{name}: {lid} is a VPC endpoint (billed per hour)")
        if rtype in ("AWS::Lambda::Version", "AWS::Lambda::Alias") and props.get("ProvisionedConcurrencyConfig"):
            problems.append(f"{name}: {lid} has provisioned concurrency")
        if rtype == "AWS::Lambda::Function" and props.get("VpcConfig"):
            problems.append(f"{name}: {lid} runs in a VPC (NAT or endpoints would be needed)")
        tags = props.get("Tags")
        if isinstance(tags, list):
            keys = {t.get("Key") for t in tags if isinstance(t, dict)}
            missing = [k for k in _TAG_KEYS if k not in keys and not (k == "logical-role" and rtype in _PARENT_ATTRIBUTED)]
            if missing:
                problems.append(f"{name}: {lid} ({rtype}) lacks cost tags {missing}")
    return problems


def gate_cost(ctx: Any) -> list[str]:
    problems: list[str] = []
    for path in templates_of(_need_assembly(ctx)):
        problems += cost_problems(_load(path), path.name)
    return problems


def gate_lambda_bundle(ctx: Any) -> list[str]:
    from infra.stacks.lambda_code import bundle_problems

    problems: list[str] = []
    for asset in sorted(p for p in _need_assembly(ctx).rglob("asset.*") if p.is_dir()):
        manifest = asset / "bundle-manifest.json"
        if not manifest.is_file():
            ctx.note("lambda-bundle", f"{asset.name}: source-only asset (local synth; the build stage refuses it in release mode)")
            continue
        from scripts.lambda_bundle import LAMBDA_PLATFORM, foreign_binaries

        problems += [f"Lambda code {asset.name}: {p}" for p in bundle_problems(asset)]
        platform = json.loads(manifest.read_text(encoding="utf-8")).get("python_platform")
        if platform != LAMBDA_PLATFORM:
            problems.append(f"Lambda code {asset.name}: built for {platform}, the functions run on {LAMBDA_PLATFORM} (arm64)")
        problems += [f"Lambda code {asset.name}: {b} is not an arm64 binary" for b in foreign_binaries(asset)]
    return problems


def log_retention_problems(template: dict[str, Any], name: str, days: int = 30, *, bootstrap_managed: Iterable[str] = ()) -> list[str]:
    """Lesson L6: every log group has the retention; every function and CodeBuild project has a group.

    A CodeBuild project whose group the bootstrap manages (contract gap ``pipeline-logs``,
    :func:`scripts.bootstrap.apply_codebuild_log_retention`) is accepted by its project name."""
    problems: list[str] = []
    groups = _resources(template, "AWS::Logs::LogGroup")
    covered: set[str] = set()
    literal_names: set[str] = set()
    for lid, g in sorted(groups.items()):
        props = g.get("Properties") or {}
        if props.get("RetentionInDays") != days:
            problems.append(f"{name}: log group {lid} retention is {props.get('RetentionInDays')}, not {days} days")
        covered |= _refs(props.get("LogGroupName"))
        if isinstance(props.get("LogGroupName"), str):
            literal_names.add(props["LogGroupName"])
    managed = set(bootstrap_managed)
    for lid, fn in sorted(_resources(template, "AWS::Lambda::Function").items()):
        fname = (fn.get("Properties") or {}).get("FunctionName")
        if lid not in covered and f"/aws/lambda/{fname}" not in literal_names:
            problems.append(f"{name}: function {lid} has no explicit {days}-day log group (lesson L6)")
    for lid, proj in sorted(_resources(template, "AWS::CodeBuild::Project").items()):
        pname = (proj.get("Properties") or {}).get("Name")
        if lid in covered or f"/aws/codebuild/{pname}" in literal_names or pname in managed:
            continue
        problems.append(f"{name}: CodeBuild project {lid} has no {days}-day log group in IaC or in the bootstrap (lesson L6)")
    return problems


def gate_log_retention(ctx: Any) -> list[str]:
    from infra.stacks.pipeline import codebuild_project_names

    managed = codebuild_project_names()
    problems: list[str] = []
    for path in templates_of(_need_assembly(ctx)):
        t = _load(path)
        if _resources(t, "AWS::CodeBuild::Project") and not _resources(t, "AWS::Logs::LogGroup"):
            ctx.note("log-retention", f"{path.name}: CodeBuild log retention applied by the bootstrap (contract gap pipeline-logs)")
        problems += log_retention_problems(t, path.name, bootstrap_managed=managed)
    return problems


def _principal_text(principal: Any) -> str:
    return json.dumps(principal, sort_keys=True)


def grant_problems(template: dict[str, Any], name: str, env: str) -> list[str]:
    """ENVW-03/04/08 over one environment's tool template."""
    from finplan_tools.core.registry import CATALOG

    problems: list[str] = []
    functions = _resources(template, "AWS::Lambda::Function")
    aliases = _resources(template, "AWS::Lambda::Alias")
    tool_of_fn = {}
    for lid, fn in functions.items():
        tool = ((fn.get("Properties") or {}).get("Environment") or {}).get("Variables", {}).get("FINPLAN_TOOL_NAME")
        if tool:
            tool_of_fn[lid] = tool
    tool_of_alias = {lid: tool_of_fn.get(next(iter(_refs((a.get("Properties") or {}).get("FunctionName"))), "")) for lid, a in aliases.items()}
    stage_role = f"role/finplan-{env}-{REPO}-pipeline-stage-role"
    for lid, perm in sorted(_resources(template, "AWS::Lambda::Permission").items()):
        props = perm.get("Properties") or {}
        principal = _principal_text(props.get("Principal"))
        target = _refs(props.get("FunctionName"))
        alias_ids = [t for t in target if t in aliases]
        if not alias_ids:
            problems.append(f"{name}: {lid} grants on the function, not on the alias")
            continue
        tool = tool_of_alias.get(alias_ids[0])
        if props.get("Action") != "lambda:InvokeFunction":
            problems.append(f"{name}: {lid} grants {props.get('Action')}")
        if "*" in principal or props.get("Principal") in ("*", None):
            problems.append(f"{name}: {lid} has a wildcard principal")
        kind = "stage" if stage_role in principal else "direct" if "DirectTestPrincipal" in principal else "gateway" if "GatewayPrincipalRoleName" in principal else None
        if kind is None:
            problems.append(f"{name}: {lid} grants to an unexpected principal {principal}")
            continue
        if kind in ("direct", "gateway") and not perm.get("Condition"):
            problems.append(f"{name}: {lid} ({kind}) is not conditional on its SSM-resolved parameter")
        if env == "prod" and kind in ("stage", "direct") and tool and CATALOG[tool].state_changing:
            problems.append(f"{name}: {lid} lets the prod {kind} principal invoke the state-changing tool {tool}")
    return problems


def gate_invoke_grants(ctx: Any) -> list[str]:
    problems: list[str] = []
    for path in templates_of(_need_assembly(ctx)):
        t = _load(path)
        env = _env_of(t)
        if env in ENVIRONMENTS and _resources(t, "AWS::Lambda::Function"):
            problems += grant_problems(t, path.name, env)
    return problems


def wiring_problems(template: dict[str, Any], name: str, env: str) -> list[str]:
    """ENVW-01: only this environment's (or shared) SSM paths and resource names, no literals."""
    text = json.dumps(template)
    problems: list[str] = []
    # other environments may appear only inside the explicit DenyOtherEnvironments statements
    stripped = json.dumps(_strip_denies(template))
    for other in ENVIRONMENTS:
        if other != env and (f"finplan-{other}-" in stripped or f"/finplan/{other}" in stripped):
            problems.append(f"{name}: names a finplan-{other}-* resource or path outside an explicit deny")
    if _LITERAL.search(text):
        problems.append(f"{name}: contains a literal account ID or endpoint")
    return problems


def _strip_denies(node: Any) -> Any:
    if isinstance(node, dict):
        if node.get("Effect") == "Deny":
            return {}
        return {k: _strip_denies(v) for k, v in node.items()}
    if isinstance(node, list):
        return [_strip_denies(i) for i in node]
    return node


def gate_env_wiring(ctx: Any) -> list[str]:
    problems: list[str] = []
    for path in templates_of(_need_assembly(ctx)):
        t = _load(path)
        env = _env_of(t)
        if env in ENVIRONMENTS and _resources(t, "AWS::Lambda::Function"):
            problems += wiring_problems(t, path.name, env)
    return problems


def gate_cdk_bootstrap_free(ctx: Any) -> list[str]:
    problems: list[str] = []
    for path in sorted(_need_assembly(ctx).rglob("*.json")):
        if path.name in ("tree.json", "validation-report.json"):
            continue
        m = _CDK_BOOTSTRAP.search(path.read_text(encoding="utf-8", errors="replace"))
        if m:
            problems.append(f"{path.name}: references the CDK bootstrap ({m.group(0)}); stacks must deploy without CDKToolkit (lesson L1)")
    return problems


INFRA_GATES: list[tuple[str, str, Callable[[Any], list[str]]]] = [
    ("ownership", "post", gate_ownership),
    ("boundaries", "post", gate_boundaries),
    ("live-perm-synth", "post", gate_live_perms_synth),
    ("pipeline-structure", "post", gate_pipeline_structure),
    ("cost", "post", gate_cost),
    ("lambda-bundle", "post", gate_lambda_bundle),
    ("log-retention", "post", gate_log_retention),
    ("invoke-grants", "post", gate_invoke_grants),
    ("env-wiring", "post", gate_env_wiring),
    ("cdk-bootstrap-free", "post", gate_cdk_bootstrap_free),
]


def run(ctx: Any, names: Iterable[str] | None = None) -> dict[str, list[str]]:
    """Run the infra gates directly (tests)."""
    wanted = set(names or [g[0] for g in INFRA_GATES])
    return {name: fn(ctx) for name, _stage, fn in INFRA_GATES if name in wanted}
