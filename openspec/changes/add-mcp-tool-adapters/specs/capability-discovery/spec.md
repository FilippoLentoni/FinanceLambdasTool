# Spec Delta

## Purpose

Defines the `describe_capabilities` tool, which tells agents and direct callers which tools exist in an environment, which contract versions and release they belong to, and whether each tool's producer dependencies are available.

## ADDED Requirements

### Requirement: Capability description response
`describe_capabilities` SHALL return a response that validates against the contract capability-description schema. It lists environment, `release_id`, the pinned contract version and served majors, and for each tool its name, input/output schema `$id`s, read-only or state-changing flag and availability.

#### Scenario: Phase 1 beta listing
- **WHEN** `describe_capabilities` is invoked in beta
- **THEN** every tool in the tool catalog is listed with its schema `$id`s, the current beta `release_id` and the pinned contract version

### Requirement: Dependency availability per tool
Each tool entry SHALL report `available`, or `unavailable` with reason `DEPENDENCY_UNAVAILABLE`, based on whether that tool's producer reference resolves in the same environment. Availability MUST be computed from configuration and release manifests, not hard-coded.

#### Scenario: Model service absent
- **WHEN** no FinanceModel release manifest exists in beta
- **THEN** `submit_experiment`, `get_job_status` and `get_experiment_result` are reported `unavailable` with reason `DEPENDENCY_UNAVAILABLE`, and the plan and market-data tools are `available`

#### Scenario: Producer serves incompatible major
- **WHEN** the platform release in gamma serves only a contract major different from the tool release's pinned major
- **THEN** the plan and market-data tools are reported `unavailable` with reason `UNSUPPORTED_CONTRACT_VERSION`

### Requirement: Declared limits and capabilities are reported, not inferred
`describe_capabilities` SHALL report the limits that apply in that environment: response byte limit, tool budget limits, supported experiment types and supported market-data granularities. Each value MUST come from producer-declared capabilities or configuration, never inferred.

#### Scenario: Per-category tool budget limits
- **WHEN** `describe_capabilities` is invoked with the default `tool-limits`
- **THEN** it reports the per-call limits by budget category (`cpu_research` USD 1.00, `gpu` USD 5.00, others 0) as configured, and notes that GPU runs require user approval

#### Scenario: Intraday not declared
- **WHEN** the platform's provider declares only `daily` granularity
- **THEN** `describe_capabilities` lists `daily` only, and does not list `intraday`

### Requirement: No side effects and no secrets
`describe_capabilities` SHALL be read-only, require no `idempotency_key` and expose no endpoint URLs, ARNs, account identifiers, role names or secret references.

#### Scenario: Leak check
- **WHEN** the conformance suite runs `describe_capabilities` in every environment
- **THEN** the response contains no ARN, account-ID, endpoint-URL or bucket-name pattern
