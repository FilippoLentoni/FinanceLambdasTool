# Spec Delta

## Purpose

Defines what FinanceLambdasTool publishes in each environment so that FinanceAgent can register Gateway targets: Lambda references, a tool catalog with schema and contract versions, and the release manifest. It also covers how these references move through the repo pipeline.

## ADDED Requirements

### Requirement: Lambda reference per tool
Each deployment SHALL publish one SSM parameter per tool at `/finplan/<env>/financelambdastool/lambda/<tool-name>-arn`, with the tool name in kebab-case. It holds a version- or alias-qualified reference to the deployed Lambda.

#### Scenario: Refresh tool reference in gamma
- **WHEN** release `rel_X` deploys to gamma
- **THEN** `/finplan/gamma/financelambdastool/lambda/refresh-market-data-arn` holds the reference to the `rel_X` version of the gamma Lambda

### Requirement: Tool catalog
Each deployment SHALL publish a tool catalog at `/finplan/<env>/financelambdastool/contract/tool-catalog`. For each tool it lists the name, description, input and output schema `$id`s, state-changing flag and Lambda reference parameter name, and for the catalog the pinned contract version and `release_id`. It MUST validate against the contract package.

#### Scenario: FinanceAgent registers tools
- **WHEN** the FinanceAgent gamma pipeline reads the gamma tool catalog
- **THEN** each entry names a Lambda reference parameter under `/finplan/gamma/financelambdastool/lambda/`, and its schema `$id`s resolve in the pinned contract version

### Requirement: Release manifest
Each deployment SHALL write `/finplan/<env>/financelambdastool/release/manifest` and `/finplan/<env>/financelambdastool/release/current-release-id`. The manifest MUST include the contract manifest fields, with `served_contract_majors` and an `outputs` map that lists every Lambda reference and the tool catalog.

#### Scenario: Manifest after beta deploy
- **WHEN** release `rel_X` deploys to beta
- **THEN** the beta manifest records `rel_X`, the pinned contract version and digest, the previous beta release ID and the outputs map

### Requirement: Same artifact promoted across environments
The build stage SHALL produce one digest-addressed Lambda artifact per commit. Beta, gamma and prod MUST deploy that artifact unchanged, with only environment configuration differing.

#### Scenario: Digest equality
- **WHEN** release `rel_X` reaches prod
- **THEN** the prod manifest's `artifact_digest` equals the beta and gamma digests for `rel_X`

### Requirement: Pipeline stages and gates
The repository pipeline SHALL run source, build and test, beta deploy and integration tests, gamma deploy and tests, manual approval, and prod deploy and smoke tests. Build and test MUST include unit tests, contract conformance against the pinned package, leak and live-permission scans, the tool-inventory check and the cost check.

#### Scenario: Conformance failure
- **WHEN** a tool's fixture response fails the pinned output schema in the build stage
- **THEN** the pipeline stops before beta

#### Scenario: Gamma failure
- **WHEN** the gamma direct-invocation conformance suite fails
- **THEN** the pipeline stops before approval and prod is unchanged

### Requirement: One-time pipeline bootstrap
The pipeline SHALL be created once by a bootstrap using the user's existing authenticated AWS CLI session; it MUST NOT require a scoped human role or refuse a root caller. It MUST check STS account and region, reuse an existing AVAILABLE CodeConnection via `/finplan/shared/financelambdastool/config/codeconnection-ref`, create the scoped automation roles and pass a source-stage dry run before enabling deploy stages.

#### Scenario: Bootstrap with the existing CLI session
- **WHEN** the bootstrap IaC is implemented and the bootstrap is run with the user's authenticated CLI session under the in-principle approval
- **THEN** it first lists the exact stacks to be created and a cost estimate, then proceeds, creates only scoped automation roles for later deploys, and prints a recommendation to move to a scoped/MFA operator role later

#### Scenario: Connection lacks repo access
- **WHEN** the source-stage dry run cannot read `FilippoLentoni/FinanceLambdasTool`
- **THEN** no deploy stage is enabled, and the bootstrap tells the user to extend the GitHub App installation to the repo and rerun the dry run

#### Scenario: No connection or account literal committed
- **WHEN** the leak scan runs over the bootstrap code and configuration templates
- **THEN** it finds no connection identifier, ARN or account ID, only the SSM parameter name

### Requirement: Bootstrap runs under the in-principle approval
The user approved the one-time bootstrap in principle on 2026-10-07. The bootstrap SHALL run only after its IaC is implemented, MUST show the exact stacks it will create and a cost estimate before making any change, and MUST NOT run during spec work.

#### Scenario: Stacks and cost shown before changes
- **WHEN** the bootstrap is started
- **THEN** it prints the exact stacks to be created and a cost estimate before creating any resource

### Requirement: Promotion requires compatible producers
Before deploying to an environment, the pipeline SHALL check that the platform release recorded there serves this release's pinned contract major. If it does not, promotion MUST be blocked. If FinanceModel is absent or incompatible, deployment MAY proceed with the experiment tools reported unavailable.

#### Scenario: Platform absent
- **WHEN** no compatible FinancialPlanning release exists in gamma
- **THEN** the gamma deploy stage fails with a dependency-missing message

#### Scenario: Model service absent
- **WHEN** a compatible platform release exists in beta but no FinanceModel release does
- **THEN** the beta deploy proceeds, and `describe_capabilities` reports the experiment tools as `DEPENDENCY_UNAVAILABLE`

### Requirement: Rollback by release identifier
The pipeline SHALL redeploy a recorded `release_id`'s stored artifact without rebuilding. It republishes the Lambda references, tool catalog and a manifest that records `rolled_back_from`.

#### Scenario: Prod rollback
- **WHEN** prod smoke tests fail after deploying `rel_Y`
- **THEN** `rel_X` is redeployed, the Lambda references point to the `rel_X` versions, and the manifest records `rolled_back_from` `rel_Y`

### Requirement: Prod smoke tests are non-mutating or synthetic
Prod smoke tests SHALL invoke `describe_capabilities` and read-only tools against the platform's synthetic prod portfolio. They MUST NOT submit paid jobs or publish plans for non-synthetic portfolios. The synthetic requirement applies to the records the smoke touches (portfolios, plans), not to market data: prod snapshots may be real phase 2 data or still synthetic during the transition, and both are accepted (decision 26).

#### Scenario: Prod smoke run
- **WHEN** the prod smoke stage runs
- **THEN** it calls only read-only tools against synthetic records, and no FinanceModel run is created
