"""FinanceLambdasTool account-level stacks (environment ``shared``; task 9.6), deployed **only** by the
authenticated bootstrap (``scripts/bootstrap.py``, ``docs/bootstrap.md``), never by the pipeline.

Mirrors the deployed FinancialPlanning and FinanceModel tooling pattern (contracts D6, D11, D13):

* ``finplan-shared-financelambdastool-pipeline-store`` (:class:`StoreStack`): the pipeline store bucket
  ``finplan-shared-financelambdastool-pipeline-store-<account>`` (CodePipeline artifacts, the
  content-addressed CDK file assets under ``assets/``, the release ledger under ``releases/``, the
  staged tooling template under ``bootstrap/``). SSE-S3, TLS only, Block Public Access, versioned,
  retained on deletion. It is small and asset-free, so it uses :class:`aws_cdk.LegacyStackSynthesizer`:
  the CLI deploys it inline with the operator's credentials and references **no** ``cdk-hnb659fds``
  role and **no** ``cdk-*-assets`` bucket (lesson L1).
* ``finplan-shared-financelambdastool-tooling`` (:class:`ToolingStack`): the pipeline, its roles and
  its CodeBuild projects (:mod:`infra.stacks.pipeline`). It uses a
  :class:`aws_cdk.CliCredentialsStackSynthesizer` that stages the template in the store under
  ``bootstrap/`` with the operator's CLI credentials (no ``CDKToolkit``; lesson L1).

FinanceLambdasTool creates **no** permission boundary, **no** budget and **no** CodeConnection:
the boundaries and the project budget belong to the FinancialPlanning tooling stack, and the
connection is the existing one, referenced through
``/finplan/shared/financelambdastool/config/codeconnection-ref`` (lesson L6). It only publishes the
role names the platform's budget action must deny (``config/budget-enforced-role-names``).

Environment stacks deployed by the pipeline use :func:`deployment_synthesizer`: the Lambda code asset
lives in the store under ``assets/<sha256>.zip`` (published by the build stage), and the templates
carry no bootstrap-version rule and no CDK role ARN.
"""

from __future__ import annotations

from typing import Any

import aws_cdk as cdk
import jsii
from aws_cdk import Aws, Duration
from aws_cdk import aws_iam as iam
from aws_cdk import aws_s3 as s3
from constructs import Construct
from finplan_contracts import boundaries as contract_boundaries
from finplan_contracts import ssm as contract_ssm

from finplan_tools.core.config import REGION

from . import naming as n
from .common import shared_tags, tag_role

__all__ = [
    "ASSET_PREFIX",
    "BOOTSTRAP_PREFIX",
    "RELEASES_PREFIX",
    "STORE_CONSTRUCT_ID",
    "STORE_STACK_NAME",
    "TOOLING_CONSTRUCT_ID",
    "TOOLING_STACK_NAME",
    "StoreStack",
    "ToolingStack",
    "deployment_synthesizer",
    "get_tooling_stack",
    "tooling_synthesizer",
]

TOOLING_CONSTRUCT_ID = "Tooling"
TOOLING_STACK_NAME = n.shared_name("tooling")
STORE_CONSTRUCT_ID = "PipelineStore"
STORE_STACK_NAME = n.shared_name("pipeline-store")
ASSET_PREFIX = "assets/"
BOOTSTRAP_PREFIX = "bootstrap/"
RELEASES_PREFIX = "releases/"
CACHE_PREFIX = "cache/"
STORE_PREFIX_EXPIRY_DAYS = 30
STORE_NONCURRENT_EXPIRY_DAYS = 7
QUALIFIER = "finplan"
#: No container images exist in this repository; naming a repository keeps CDK's default
#: ``cdk-hnb659fds-container-assets-*`` name out of every asset manifest (lesson L1).
NO_IMAGE_REPOSITORY = n.shared_name("no-container-images")


def _store_synth(prefix: str) -> cdk.CliCredentialsStackSynthesizer:
    return cdk.CliCredentialsStackSynthesizer(
        file_assets_bucket_name=n.pipeline_store_bucket_name("${AWS::AccountId}"),
        bucket_prefix=prefix,
        image_assets_repository_name=NO_IMAGE_REPOSITORY,
        qualifier=QUALIFIER,
    )


@jsii.implements(cdk.IReusableStackSynthesizer)
class _PerStackSynthesizer:
    """Binds a fresh synthesizer to every stack (a shared instance would share one asset-manifest
    builder across stacks in this CDK version)."""

    def reusable_bind(self, stack: cdk.Stack) -> cdk.IBoundStackSynthesizer:
        synth = _store_synth(ASSET_PREFIX)
        synth.bind(stack)
        return synth


def deployment_synthesizer() -> cdk.IReusableStackSynthesizer:
    """Synthesizer of the environment stacks the pipeline deploys."""
    return _PerStackSynthesizer()


def tooling_synthesizer() -> cdk.CliCredentialsStackSynthesizer:
    """The tooling template is staged in the store (``bootstrap/``) with the operator's CLI credentials."""
    return _store_synth(BOOTSTRAP_PREFIX)


class StoreStack(cdk.Stack):
    def __init__(self, scope: Construct, construct_id: str, **kwargs: Any) -> None:
        super().__init__(
            scope,
            construct_id,
            stack_name=STORE_STACK_NAME,
            env=cdk.Environment(region=REGION),
            synthesizer=cdk.LegacyStackSynthesizer(),
            termination_protection=True,
            description="FinanceLambdasTool pipeline store (environment shared): pipeline artifacts, content-addressed CDK assets, release ledger. Deployed only by the authenticated bootstrap.",
            **kwargs,
        )
        shared_tags(self)
        self.bucket = s3.Bucket(
            self,
            "Store",
            bucket_name=n.pipeline_store_bucket_name(Aws.ACCOUNT_ID),
            encryption=s3.BucketEncryption.S3_MANAGED,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            object_ownership=s3.ObjectOwnership.BUCKET_OWNER_ENFORCED,
            enforce_ssl=True,
            versioned=True,
            removal_policy=cdk.RemovalPolicy.RETAIN,
            lifecycle_rules=[
                # CodePipeline stores artifacts under the first 20 characters of the pipeline name
                s3.LifecycleRule(id="pipeline-artifacts", prefix=n.PIPELINE_NAME[:20] + "/", expiration=Duration.days(STORE_PREFIX_EXPIRY_DAYS)),
                s3.LifecycleRule(id="build-cache", prefix=CACHE_PREFIX, expiration=Duration.days(14)),
                s3.LifecycleRule(id="assets", prefix=ASSET_PREFIX, expiration=Duration.days(STORE_PREFIX_EXPIRY_DAYS)),
                s3.LifecycleRule(id="bootstrap", prefix=BOOTSTRAP_PREFIX, expiration=Duration.days(STORE_PREFIX_EXPIRY_DAYS)),
                s3.LifecycleRule(id="noncurrent", noncurrent_version_expiration=Duration.days(STORE_NONCURRENT_EXPIRY_DAYS), abort_incomplete_multipart_upload_after=Duration.days(7)),
            ],
        )
        tag_role(self.bucket, "pipeline-artifact-bucket")


class ToolingStack(cdk.Stack):
    def __init__(self, scope: Construct, construct_id: str, **kwargs: Any) -> None:
        super().__init__(
            scope,
            construct_id,
            stack_name=TOOLING_STACK_NAME,
            env=cdk.Environment(region=REGION),
            synthesizer=tooling_synthesizer(),
            termination_protection=True,
            description="FinanceLambdasTool account-level tooling (environment shared): the pipeline, its scoped roles and CodeBuild projects. Deployed only by the authenticated bootstrap. No budget, boundary or connection (FinancialPlanning-owned or existing).",
            **kwargs,
        )
        shared_tags(self)
        #: Account-level roles the platform's budget action must deny (written by the bootstrap to
        #: /finplan/shared/financelambdastool/config/budget-enforced-role-names).
        self.enforced_roles: list[iam.Role] = []
        boundary = iam.ManagedPolicy.from_managed_policy_name(self, "SharedBoundary", contract_boundaries.boundary_name(contract_ssm.SHARED))
        iam.PermissionsBoundary.of(self).apply(boundary)

    def register_enforced_role(self, role: iam.Role) -> None:
        self.enforced_roles.append(role)

    @staticmethod
    def environment_boundary(scope: Construct, env: str) -> iam.IManagedPolicy:
        return iam.ManagedPolicy.from_managed_policy_name(scope, f"{env.capitalize()}Boundary", contract_boundaries.boundary_name(env))


def get_tooling_stack(app: cdk.App) -> ToolingStack:
    existing = app.node.try_find_child(TOOLING_CONSTRUCT_ID)
    if existing is not None:
        assert isinstance(existing, ToolingStack)
        return existing
    store = app.node.try_find_child(STORE_CONSTRUCT_ID) or StoreStack(app, STORE_CONSTRUCT_ID)
    tooling = ToolingStack(app, TOOLING_CONSTRUCT_ID)
    tooling.add_stack_dependency(store)  # the CLI stages the tooling template in the store
    return tooling
