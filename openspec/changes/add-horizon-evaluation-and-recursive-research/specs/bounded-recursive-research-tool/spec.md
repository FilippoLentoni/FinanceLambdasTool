## ADDED Requirements

### Requirement: Recursive research MCP adapter
The system SHALL expose run_recursive_improvement using the same-environment model API and retain immutable cycle and iteration evidence references.

#### Scenario: Read-only research cycle
- **WHEN** a caller omits dry_run or supplies dry_run=true
- **THEN** the adapter returns producer evidence without paid authorization or launching compute.

### Requirement: Bounded paid research
Paid recursive advancement SHALL require a verified researcher identity, confirmed_by_user=true, downstream idempotency and a producer estimate within category budgets and the USD 0.50 CPU hard bound.

#### Scenario: Estimate exceeds the hard bound
- **WHEN** dry_run=false preflight exceeds USD 0.50
- **THEN** the adapter returns BUDGET_EXCEEDED without submitting paid work.

### Requirement: Preserve horizon evidence
The existing decision evaluation adapter SHALL preserve producer horizon objectives, contract provenance, replay and control status without generating market or neural attribution claims.

#### Scenario: Partial evaluation returned
- **WHEN** the model returns partial or unavailable horizon evidence
- **THEN** the remote MCP returns that evidence unchanged for the hosted and direct client.
