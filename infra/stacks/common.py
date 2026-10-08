"""Shared CDK building blocks of the FinanceLambdasTool stacks.

* :class:`EnvStack` - base class of the per-environment stacks: the contract cost-allocation tags
  (``project``, ``owner-repo`` = ``financelambdastool``, ``environment``) on everything in the
  stack, the environment permission boundary ``finplan-<env>-permission-boundary`` (created by the
  FinancialPlanning tooling stack) on **every** IAM role in it, and termination protection in prod.
  Each resource still needs its own ``logical-role``: call :func:`tag_role` (taggable types) or
  :func:`metadata_role` (types CloudFormation cannot tag).
* :func:`shared_tags` - the same tags with ``environment=shared`` for the account-level stacks.
"""

from __future__ import annotations

from typing import Any

import aws_cdk as cdk
from aws_cdk import Stack, Tags
from aws_cdk import aws_iam as iam
from constructs import Construct, IConstruct
from finplan_contracts import boundaries as contract_boundaries
from finplan_contracts import ssm as contract_ssm

from finplan_tools.core.config import REGION

from .naming import REPO

__all__ = ["EnvStack", "metadata_role", "shared_tags", "tag_role"]


def tag_role(construct: IConstruct, logical_role: str) -> None:
    """Set the ``logical-role`` tag (ownership-matrix key) on a construct tree."""
    Tags.of(construct).add("logical-role", logical_role)


def metadata_role(resource: cdk.CfnResource, logical_role: str) -> None:
    """Ownership attribution for types that cannot carry tags (the ownership check reads ``Metadata``)."""
    resource.add_metadata("logical-role", logical_role)


def _base_tags(stack: Stack, env: str) -> None:
    base = contract_ssm.cost_allocation_tags(REPO, env, "placeholder")
    for key in ("project", "owner-repo", "environment"):
        Tags.of(stack).add(key, base[key])


def shared_tags(stack: Stack) -> None:
    _base_tags(stack, contract_ssm.SHARED)


class EnvStack(Stack):
    """Per-environment stack: contract tags, environment permission boundary, prod protection."""

    def __init__(self, scope: Construct, construct_id: str, *, env_name: str, description: str, **kwargs: Any) -> None:
        super().__init__(scope, construct_id, env=cdk.Environment(region=REGION), description=description, termination_protection=env_name == "prod", **kwargs)
        self.env_name = env_name
        _base_tags(self, env_name)
        boundary = iam.ManagedPolicy.from_managed_policy_name(self, "EnvPermissionBoundary", contract_boundaries.boundary_name(env_name))
        iam.PermissionsBoundary.of(self).apply(boundary)
