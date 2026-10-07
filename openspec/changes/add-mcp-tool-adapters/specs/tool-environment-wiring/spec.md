# Spec Delta

## Purpose

Defines how each environment's tool Lambdas resolve their producers, who may invoke them, and how isolation is kept. Gamma tools reach only gamma platform and model services, colleagues can invoke Lambdas directly before the Gateway exists, and local tests run without credentials.

## ADDED Requirements

### Requirement: Producer references resolved from same-environment configuration
Each tool Lambda SHALL resolve platform and FinanceModel references only from SSM parameters and release manifests under its own environment segment, `/finplan/<env>/...`. Source code, environment variables and Lambda configuration MUST contain no literal endpoint, ARN or account identifier.

#### Scenario: Gamma wiring
- **WHEN** the gamma tool Lambdas start
- **THEN** every producer reference they use resolves from `/finplan/gamma/financialplanning/...` or `/finplan/gamma/financemodel/...`

#### Scenario: Literal committed
- **WHEN** a commit adds an endpoint URL or ARN literal to tool source or IaC
- **THEN** the build-stage identifier scan fails

### Requirement: Cross-environment calls are impossible
Beta, gamma and prod share one AWS account in us-east-2. Tool Lambda roles SHALL be allowed to call only same-environment platform and model APIs and read only same-environment SSM parameters. Every role class MUST carry the contract permission boundary that denies actions on resources tagged with, or named under, another environment. A call to another environment's resource MUST be denied by policy.

#### Scenario: Gamma tool calls prod platform
- **WHEN** a gamma tool role attempts to invoke the prod plan API
- **THEN** the call is denied and the gamma isolation test records the denial as a pass

#### Scenario: Role class without permission boundary
- **WHEN** the synthesized template defines a tool role without the environment permission boundary
- **THEN** the build-stage policy check fails

### Requirement: Explicit invoke permissions
Each tool Lambda SHALL grant invoke permission only to principals named in its environment's configuration: the FinanceAgent Gateway principal for that environment, once published, and the configured direct-test principals. Wildcard or cross-environment grants MUST NOT exist.

#### Scenario: Gateway not yet published
- **WHEN** FinanceLambdasTool deploys to beta before FinanceAgent has published a beta Gateway principal
- **THEN** the Lambdas deploy with only the direct-test grants, and no Gateway grant is created

#### Scenario: Gamma Gateway invokes prod tool
- **WHEN** the gamma Gateway principal invokes a prod tool Lambda
- **THEN** the invocation is denied

### Requirement: Direct invocation before Gateway
Colleagues SHALL be able to invoke each tool Lambda directly in beta and gamma with contract fixtures, using a configured direct-test principal. In prod, direct invocation MUST be limited to the pipeline smoke-test principal.

#### Scenario: Direct beta invocation
- **WHEN** a colleague with the beta direct-test role invokes `get_plan_version` with a fixture `plan_version_id`
- **THEN** the response validates against the pinned output schema without the Gateway

#### Scenario: Direct prod invocation by colleague
- **WHEN** a non-smoke principal invokes a prod tool Lambda directly
- **THEN** the invocation is denied

### Requirement: Credential-free local execution
Each tool handler SHALL run locally against an in-process mock platform and mock job backend loaded from contract fixtures, needing no AWS credentials or network. The mock backends MUST be excluded from deployable artifacts.

#### Scenario: Offline fixture run
- **WHEN** a developer runs `submit_experiment` locally with a package fixture and no AWS credentials
- **THEN** the call returns a fixture `run_id` from the mock backend, and the response validates against the pinned schema

#### Scenario: Mock in deployed artifact
- **WHEN** the build stage inspects the deployable Lambda artifact
- **THEN** it fails if the mock backend module or fixture-minting code is present

### Requirement: Phase 1 fixture-backed deployment
In phase 1, deployed tools SHALL be backed by the platform's fixture data and synthetic portfolios and by FinanceModel's CPU fixture stub job. Tools MUST make no live market-data provider call of their own and start no GPU compute.

#### Scenario: Phase 1 experiment in gamma
- **WHEN** `submit_experiment` runs in gamma during phase 1
- **THEN** it reaches only the gamma FinanceModel fixture stub job type, and any other job type is reported unavailable

### Requirement: Near-zero standing cost
Tool resources SHALL be on-demand only. The stack MUST NOT include provisioned concurrency, VPC NAT gateways, always-on compute or endpoints, and every taggable resource MUST carry the contract cost-allocation tags.

#### Scenario: Provisioned concurrency introduced
- **WHEN** the synthesized template configures provisioned concurrency or a NAT gateway
- **THEN** the build-stage cost check fails
