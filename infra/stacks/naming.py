"""Names of every FinanceLambdasTool resource (task groups 8 and 9; contracts naming
``finplan-<env>-<repo>-<logical>``).

One place for the names that the IaC, the release publisher, the pre-deploy step, the bootstrap, the
producers' grants and the tests must agree on. Names only: no account ID, ARN or endpoint is ever
written to a file. Values that need the account (bucket names, ARNs) are built at deploy time from
CloudFormation pseudo parameters (``${AWS::AccountId}``) or at run time from the caller's own account.

Producer grants rely on these names:

* FinancialPlanning ``config/<env>.json`` ``consumer_principals`` admits the role classes by the name
  patterns ``finplan-<env>-financelambdastool-tool-role-<class>*`` (:func:`role_class_role_name`);
* the FinanceModel job API resource policy admits ``finplan-<env>-financelambdastool-*``.
"""

from __future__ import annotations

from finplan_contracts import boundaries as contract_boundaries
from finplan_contracts import ssm as contract_ssm

from finplan_tools.core.registry import CATALOG, ROLE_CLASSES

__all__ = [
    "ENVIRONMENTS",
    "LOG_RETENTION_DAYS",
    "PIPELINE_NAME",
    "REPO",
    "ROLE_CLASSES",
    "ROLE_CLASS_LOGICAL_ROLES",
    "TOOLS_STACK",
    "TOOL_ALIAS",
    "deploy_role_name",
    "env_name",
    "exec_role_name",
    "function_name",
    "own_ssm",
    "pipeline_store_bucket_name",
    "role_class_role_name",
    "shared_name",
    "stack_name",
    "stage_role_name",
    "tool_logical",
]

REPO = "financelambdastool"
ENVIRONMENTS = ("beta", "gamma", "prod")
#: Retention of every log group this repository declares (Lambda and CodeBuild; lesson L6).
LOG_RETENTION_DAYS = 30
#: Alias every published Lambda reference points at (design D9).
TOOL_ALIAS = "current"
#: The per-environment stack holding the tool Lambdas and the role classes.
TOOLS_STACK = "tools"
#: Matrix row ``mcp-adapter-lambdas`` logical roles of the three role classes.
ROLE_CLASS_LOGICAL_ROLES = {c: f"tool-role-{c}" for c in ROLE_CLASSES}


def env_name(env: str, logical: str, suffix: str | None = None) -> str:
    """``finplan-<env>-financelambdastool-<logical>[-<suffix>]``."""
    return contract_boundaries.resource_name(env, REPO, logical, suffix)


def shared_name(logical: str, suffix: str | None = None) -> str:
    """``finplan-shared-financelambdastool-<logical>[-<suffix>]``."""
    return contract_boundaries.resource_name(contract_ssm.SHARED, REPO, logical, suffix)


def _role(name: str) -> str:
    if len(name) > 64:  # pragma: no cover - guarded by a unit test over every name
        raise ValueError(f"role name {name!r} exceeds 64 characters")
    return name


def stack_name(env: str, part: str = TOOLS_STACK) -> str:
    return f"finplan-{env}-{REPO}-{part}"


def tool_logical(tool: str) -> str:
    """Kebab-case tool name (function names and SSM reference names)."""
    if tool not in CATALOG:
        raise ValueError(f"{tool!r} is not a catalog tool")
    # Keep physical Lambda names within AWS's 64-character limit in every stage.
    if tool == "explain_classical_recommendation":
        return "explain-classical-plan"
    return tool.replace("_", "-")


def function_name(env: str, tool: str) -> str:
    """``finplan-<env>-financelambdastool-<tool-kebab>``."""
    name = env_name(env, tool_logical(tool))
    if len(name) > 64:  # pragma: no cover - guarded by a unit test
        raise ValueError(f"function name {name!r} exceeds 64 characters")
    return name


def role_class_role_name(env: str, role_class: str) -> str:
    """``finplan-<env>-financelambdastool-tool-role-<class>`` (the platform's name pattern)."""
    if role_class not in ROLE_CLASSES:
        raise ValueError(f"unknown role class {role_class!r}")
    return _role(env_name(env, ROLE_CLASS_LOGICAL_ROLES[role_class]))


PIPELINE_NAME = shared_name("pipeline")


def pipeline_store_bucket_name(account: str) -> str:
    """``finplan-shared-financelambdastool-pipeline-store-<account>``; ``account`` is a token or placeholder."""
    return f"{shared_name('pipeline-store')}-{account}"


def deploy_role_name(env: str) -> str:
    """Deploy action role of ``env`` (CodePipeline assumes it); declared in the tooling stack."""
    return _role(shared_name("deploy-role", env))


def exec_role_name(env: str) -> str:
    """CloudFormation execution role of ``env``."""
    return _role(shared_name("deploy-role", f"{env}-exec"))


def stage_role_name(env: str) -> str:
    """Pre-deploy resolver, release publisher and environment test runner of ``env``."""
    return _role(env_name(env, "pipeline-stage", "role"))


def own_ssm(env: str, category: str, name: str) -> str:
    return contract_ssm.build(env, REPO, category, name)
