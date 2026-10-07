# Spec Delta

## Purpose

Defines the plan tools (`get_plan`, `get_plan_version`, `list_plan_versions`, `create_override_version`, `validate_plan_version`, `publish_plan_version`). They read plans and change them only through the platform plan lifecycle API, preserving immutable versions, publication of exact validated versions and the separation of publication from execution.

## ADDED Requirements

### Requirement: Plan tools use only the platform plan API
Every plan tool SHALL call only the platform plan lifecycle API at `/finplan/<env>/financialplanning/api/plan-endpoint`. Tools MUST NOT read or write platform tables or buckets directly and MUST NOT persist plan state.

#### Scenario: Website and agent agreement
- **WHEN** `get_plan_version` and a direct website-path platform read each fetch `pv_B` in the same environment
- **THEN** both return the same `plan_version_id`, checksum and canonical content

### Requirement: Read plan and plan version
`get_plan` SHALL return the plan's current head (`current_version_id`, `revision`) and its current publication, if any. `get_plan_version` SHALL return identifiers, lineage, status, checksum, a compact allocation summary and trusted artifact references.

#### Scenario: Read a validated version
- **WHEN** `get_plan_version` is called for a `validated` fixture version
- **THEN** the response includes `plan_id`, `parent_plan_version_id`, `input_snapshot_id`, `configuration_id`, origin, status `validated` and the checksum

#### Scenario: Unknown version
- **WHEN** the `plan_version_id` does not exist in the environment
- **THEN** the tool returns `NOT_FOUND`

### Requirement: List plan versions
`list_plan_versions` SHALL return a plan's versions newest first. Each entry has `plan_version_id`, parent, origin, status, checksum and creation time. Pagination uses a continuation token.

#### Scenario: Paginated listing
- **WHEN** a plan has more versions than the page size
- **THEN** the first response returns one page and a `next_token`, and the next call with that token returns the following page with no duplicates

### Requirement: Overrides create child versions
`create_override_version` SHALL create a child of a named parent through the platform API, passing `expected_revision` and the derived idempotency key. It returns the platform-minted `plan_version_id` and status. It MUST NOT modify the parent.

#### Scenario: Manual override
- **WHEN** a caller overrides `pv_A` with changed allocations and the current `expected_revision`
- **THEN** the tool returns a new platform-minted child `plan_version_id` with parent `pv_A` and origin `manual_override`, and `pv_A` is unchanged

#### Scenario: Concurrent override
- **WHEN** a second caller overrides using a stale `expected_revision`
- **THEN** the tool returns `CONFLICT` with `retryable` false and a hint to re-read the plan head

#### Scenario: No-effect override
- **WHEN** the submitted content equals the parent's content
- **THEN** the tool returns the new child version with `no_effect` true and a checksum equal to the parent's

### Requirement: Immutable versions are never edited
No plan tool SHALL offer an operation that edits an existing version. A request that targets an existing version for modification MUST return `IMMUTABLE_RECORD`.

#### Scenario: Attempt to edit
- **WHEN** a caller asks `create_override_version` to change `pv_A` in place, with no new child
- **THEN** the tool returns `IMMUTABLE_RECORD` and directs the caller to create a child version

### Requirement: Validate plan version
`validate_plan_version` SHALL trigger the platform's deterministic validation and return the resulting status (`validated` or `invalid`) and itemized findings. It MUST NOT change validation rules or override an `invalid` result.

#### Scenario: Weights do not reconcile
- **WHEN** a version's allocations sum to 1.07
- **THEN** the tool returns status `invalid` with the platform's reconciliation finding

### Requirement: Publish an exact validated version
`publish_plan_version` SHALL publish one named `plan_version_id` through the platform API with the publication `expected_revision` and the derived idempotency key. It returns the `publication_id` and checksum. The platform's `PRECONDITION_FAILED` for an unvalidated version MUST pass through unchanged.

#### Scenario: Publish validated version
- **WHEN** a caller publishes `validated` `pv_B`
- **THEN** the tool returns the platform-minted `publication_id` referencing `pv_B` and its checksum

#### Scenario: Publish invalid version
- **WHEN** a caller publishes a version whose status is `invalid`
- **THEN** the tool returns `PRECONDITION_FAILED` and no publication exists

#### Scenario: Duplicate publish
- **WHEN** the same caller repeats a publish with the same key and body after a timeout
- **THEN** the original `publication_id` is returned and only one publication exists

### Requirement: Publication never executes trades
Plan tools SHALL NOT create executions, place orders or call any brokerage, exchange, payment or wallet system. Publishing MUST NOT trigger an execution. No tool in this release MUST accept an execution mode.

#### Scenario: Execution requested through publish
- **WHEN** a `publish_plan_version` request includes an `execute` or `mode` `live` field
- **THEN** the tool returns `VALIDATION_FAILED` for the unknown field and makes no platform call

#### Scenario: Tool inventory check
- **WHEN** the release's tool catalog is checked in the build stage
- **THEN** it contains no execution, trading, payment or wallet tool, and the build fails if one is added

### Requirement: Synthetic portfolios only in phase 1
In phase 1, plan write tools SHALL operate only on plans whose portfolio is flagged `synthetic` true. They MUST return `OPERATION_NOT_PERMITTED` for any other portfolio.

#### Scenario: Non-synthetic portfolio
- **WHEN** `create_override_version` targets a plan whose portfolio is not synthetic
- **THEN** the tool returns `OPERATION_NOT_PERMITTED` and makes no write
