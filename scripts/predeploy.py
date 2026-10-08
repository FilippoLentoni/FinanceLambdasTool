#!/usr/bin/env python3
"""Pre-deploy step of every environment stage (tasks 6.2a, 8.2, 8.3, 9.2; REL-06, EXP-15, ENVW-01,
ENVW-03, ENVW-08). Runs as the environment's stage role, before ``DeployTools``, from BuildOutput.

It stops the stage (nothing is deployed, no invoke grant changes) when:

* the pinned contract package is not allowed in the environment (0.x is beta-only);
* the platform release recorded in **this** environment is absent or does not serve the pinned
  contract major (REL-06: "dependency missing"); FinanceModel absent or incompatible only marks the
  experiment tools unavailable (``JobApiId=none``);
* a per-call tool limit exceeds ``per_call_max_fraction`` x its category allocation in
  ``/finplan/shared/financialplanning/config/budget-allocation``, or names a category the allocation
  does not have (EXP-15, task 6.2a);
* a producer endpoint is not an API Gateway URL of this region;
* ``/finplan/<env>/financelambdastool/config/direct-test-principal-name`` holds an ARN, a wildcard,
  an account ID, the account root or more than one principal (ENVW-08). An absent parameter means
  no direct-test grant (fail closed);
* ``/finplan/<env>/financeagent/agent/gateway-principal-ref`` names a role of another account or
  another environment.

Only ``/finplan/<env>/...`` and ``/finplan/shared/...`` are read (:func:`finplan_contracts.ssm.check_read`),
so a gamma stage can only ever wire gamma producers (ENVW-01). The resolved values become the
``DeployTools`` parameter overrides through CodePipeline variables (:data:`infra.stacks.tools.PARAMETERS`).
"""

from __future__ import annotations

import json
import re
import shlex
import sys
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
for _p in (ROOT, ROOT / "src"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from finplan_contracts import budget as contract_budget
from finplan_contracts import ssm as contract_ssm

from finplan_tools.core.config import DEFAULT_TOOL_LIMITS, REGION, limit_bound_problems, tool_limits_problems
from finplan_tools.core.contracts import contract_major
from infra.stacks import naming as n
from infra.stacks.tools import NONE, PARAMETERS

__all__ = [
    "PredeployError",
    "PredeployResult",
    "compatibility_problems",
    "limit_problems",
    "normalize_direct_test_principal",
    "normalize_gateway_principal",
    "parse_endpoint",
    "resolve",
    "write_variables",
]

_ENDPOINT = re.compile(r"^https://([a-z0-9]{10})\.execute-api\.([a-z0-9-]+)\.amazonaws\.com/([A-Za-z0-9_-]{1,128})(/[A-Za-z0-9_./-]*)?\Z")
_IAM_NAME = re.compile(r"^[A-Za-z0-9+=,.@_-]{1,64}\Z")
_ACCOUNT_ID = re.compile(r"\d{12}")
_ROLE_ARN = re.compile(r"^arn:aws[a-z-]*:iam::(\d{12}):role/([A-Za-z0-9+=,.@_/-]{1,128})\Z")


class PredeployError(RuntimeError):
    def __init__(self, problems: list[str]) -> None:
        super().__init__("; ".join(problems))
        self.problems = problems


@dataclass
class PredeployResult:
    variables: dict[str, str]
    notes: list[str] = field(default_factory=list)


def _get(ssm: Any, env: str, path: str) -> str | None:
    decision = contract_ssm.check_read(path, env)
    if not decision.allowed:
        raise PredeployError([f"refused to read {path} from the {env} stage: {'; '.join(decision.reasons)}"])
    try:
        return ssm.get_parameter(Name=path)["Parameter"]["Value"]
    except Exception as exc:
        code = getattr(exc, "response", {}).get("Error", {}).get("Code") if hasattr(exc, "response") else None
        if code == "ParameterNotFound" or type(exc).__name__ == "ParameterNotFound":
            return None
        raise


# ===================================================================== validation
def parse_endpoint(url: str | None, *, region: str = REGION) -> tuple[str, str]:
    """``(api_id, stage)`` of an API Gateway invoke URL in ``region``."""
    m = _ENDPOINT.match(url or "")
    if not m:
        raise ValueError("not an API Gateway invoke URL (https://<api-id>.execute-api.<region>.amazonaws.com/<stage>[/path])")
    if m.group(2) != region:
        raise ValueError(f"the endpoint is in region {m.group(2)}, not {region}")
    return m.group(1), m.group(3)


def normalize_direct_test_principal(value: str | None) -> str:
    """``role/<name>`` or ``user/<name>`` from the SSM value, or ``none`` when absent (ENVW-08).

    Accepted values: ``<name>`` (an IAM role name), ``role/<name>`` or ``user/<name>``. Refused: ARNs,
    wildcards, account IDs, the account root and anything naming more than one principal."""
    if value is None:
        return NONE
    v = value.strip()
    problems: list[str] = []
    if not v:
        problems.append("is empty")
    if v.lower().startswith("arn:"):
        problems.append("holds an ARN; it must hold the principal NAME only")
    if any(c in v for c in "*?"):
        problems.append("contains a wildcard")
    if _ACCOUNT_ID.search(v):
        problems.append("contains an account ID")
    if any(c in v for c in ",;[]{}\"' \t\n"):
        problems.append("names more than one principal (a single name is required)")
    kind, _, name = v.partition("/") if "/" in v else ("role", "", v)
    if kind not in ("role", "user") or "/" in name:
        problems.append("must be <name>, role/<name> or user/<name>")
    if name.lower() == "root" or v.lower() in ("root", ":root") or v.lower().endswith(":root"):
        problems.append("names the account root, which is never a direct-test principal")
    if not problems and not _IAM_NAME.match(name):
        problems.append("is not a valid IAM name")
    if problems:
        raise ValueError("direct-test-principal-name " + "; ".join(dict.fromkeys(problems)))
    return f"{kind}/{name}"


def normalize_gateway_principal(value: str | None, env: str, account: str | None) -> str:
    """The Gateway service role NAME of ``env`` from ``gateway-principal-ref`` (a role ARN or a role
    name), or ``none`` when absent. Another account or another environment's role is refused."""
    if value is None or not value.strip():
        return NONE
    v = value.strip()
    m = _ROLE_ARN.match(v)
    if m:
        if account is None or m.group(1) != account:
            raise ValueError("gateway-principal-ref names a role in another account")
        name = m.group(2).rsplit("/", 1)[-1]
    elif v.startswith("arn:"):
        raise ValueError("gateway-principal-ref is not an IAM role reference")
    else:
        name = v
    if not re.fullmatch(rf"finplan-{env}-financeagent-[A-Za-z0-9+=,.@_-]{{1,40}}", name):
        raise ValueError(f"gateway-principal-ref must name this environment's FinanceAgent role (finplan-{env}-financeagent-*)")
    return name


def compatibility_problems(env: str, platform_manifest: Mapping[str, Any] | None, model_manifest: Mapping[str, Any] | None, notes: list[str]) -> list[str]:
    """REL-06: the platform is required, FinanceModel optional."""
    major = contract_major()
    problems: list[str] = []
    if not platform_manifest:
        problems.append(f"dependency missing: no compatible FinancialPlanning release in {env} (/finplan/{env}/financialplanning/release/manifest is absent)")
    else:
        served = sorted(m for m in platform_manifest.get("served_contract_majors") or [] if isinstance(m, int) and not isinstance(m, bool))
        if platform_manifest.get("environment") not in (None, env):
            problems.append(f"the platform manifest is for {platform_manifest.get('environment')}, not {env}")
        if major not in served:
            problems.append(f"dependency missing: the FinancialPlanning release in {env} serves contract majors {served}, not {major}")
    if not model_manifest:
        notes.append(f"FinanceModel has no release in {env}: the experiment tools stay DEPENDENCY_UNAVAILABLE")
    elif major not in (model_manifest.get("served_contract_majors") or []):
        notes.append(f"the FinanceModel release in {env} does not serve contract major {major}: the experiment tools stay unavailable")
    return problems


def limit_problems(limits_doc: Mapping[str, Any] | None, allocation: Mapping[str, Any] | None, notes: list[str]) -> list[str]:
    """6.2a / EXP-15: every per-call limit <= per_call_max_fraction x its category allocation."""
    doc = dict(limits_doc) if limits_doc else dict(DEFAULT_TOOL_LIMITS)
    if limits_doc is None:
        notes.append("tool-limits is absent: the code defaults apply (the bootstrap writes them)")
    alloc = dict(allocation) if allocation else dict(contract_budget.DEFAULT_ALLOCATION)
    if allocation is None:
        notes.append(f"{contract_budget.ALLOCATION_PARAMETER} is absent: bounded against the contract default allocation")
    return [f"tool-limits: {p}" for p in tool_limits_problems({**DEFAULT_TOOL_LIMITS, **doc})] + [f"per-call limit bound: {p}" for p in limit_bound_problems(doc, alloc)]


# ===================================================================== resolution
def resolve(env: str, ssm: Any, *, account: str | None, region: str = REGION) -> PredeployResult:
    """All pre-deploy checks; the CodePipeline variables of :data:`infra.stacks.tools.PARAMETERS`."""
    if env not in n.ENVIRONMENTS:
        raise PredeployError([f"unknown environment {env!r}"])
    notes: list[str] = []
    problems: list[str] = []

    def get(path: str) -> str | None:
        return _get(ssm, env, path)

    def get_json(path: str) -> Any:
        raw = get(path)
        if raw is None:
            return None
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            problems.append(f"{path} is not JSON")
            return None

    platform_manifest = get_json(f"/finplan/{env}/financialplanning/release/manifest")
    model_manifest = get_json(f"/finplan/{env}/financemodel/release/manifest")
    problems += compatibility_problems(env, platform_manifest, model_manifest, notes)
    problems += limit_problems(get_json(n.own_ssm(env, "config", "tool-limits")), get_json(contract_budget.ALLOCATION_PARAMETER), notes)

    variables = {var: NONE for var in PARAMETERS.values()}
    for prefix, path in (("PLAN", f"/finplan/{env}/financialplanning/api/plan-endpoint"), ("INGESTION", f"/finplan/{env}/financialplanning/api/ingestion-endpoint")):
        raw = get(path)
        if raw is None:
            problems.append(f"dependency missing: {path} is absent")
            continue
        try:
            variables[f"{prefix}_API_ID"], variables[f"{prefix}_API_STAGE"] = parse_endpoint(raw, region=region)
        except ValueError as exc:
            problems.append(f"{path}: {exc}")
    job = get(f"/finplan/{env}/financemodel/api/job-endpoint")
    if job is not None and model_manifest:
        try:
            variables["JOB_API_ID"], variables["JOB_API_STAGE"] = parse_endpoint(job, region=region)
        except ValueError as exc:
            problems.append(f"/finplan/{env}/financemodel/api/job-endpoint: {exc}")
    try:
        variables["DIRECT_TEST_PRINCIPAL"] = normalize_direct_test_principal(get(n.own_ssm(env, "config", "direct-test-principal-name")))
    except ValueError as exc:
        problems.append(f"{n.own_ssm(env, 'config', 'direct-test-principal-name')}: {exc}; no invoke grant was changed")
    if variables["DIRECT_TEST_PRINCIPAL"] == NONE and not any("direct-test" in p for p in problems):
        notes.append("no direct-test principal is configured: no direct-test invoke grant")
    try:
        variables["GATEWAY_PRINCIPAL_ROLE_NAME"] = normalize_gateway_principal(get(f"/finplan/{env}/financeagent/agent/gateway-principal-ref"), env, account)
    except ValueError as exc:
        problems.append(f"/finplan/{env}/financeagent/agent/gateway-principal-ref: {exc}")
    if variables["GATEWAY_PRINCIPAL_ROLE_NAME"] == NONE:
        notes.append("no Gateway principal is published: no Gateway invoke grant")
    if problems:
        raise PredeployError(problems)
    return PredeployResult(variables, notes)


def write_variables(path: Path, variables: Mapping[str, str]) -> None:
    """``NAME='value'`` lines the stage buildspec sources so CodeBuild exports them."""
    path.write_text("".join(f"{k}={shlex.quote(v)}\n" for k, v in sorted(variables.items())), encoding="utf-8")
