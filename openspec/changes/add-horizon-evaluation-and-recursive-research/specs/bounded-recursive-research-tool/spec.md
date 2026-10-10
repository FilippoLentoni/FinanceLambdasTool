## ADDED Requirements

### Requirement: Recursive research MCP adapter
The system SHALL expose run_recursive_improvement using the same-environment model API and retain immutable cycle and iteration evidence references.

#### Scenario: Read-only research cycle
- **WHEN** a caller omits dry_run or supplies dry_run=true
- **THEN** the adapter returns producer evidence without paid authorization or launching compute.

### Requirement: Bounded paid research
Paid recursive advancement SHALL require a verified researcher identity, confirmed_by_user=true, downstream idempotency and a finite nonnegative producer estimate within category budgets. Ordinary research SHALL retain the USD 0.50 CPU hard bound. Schema-valid matching Qwen/Jev benchmark proposals SHALL use their sandbox category caps and retain producer compute/vendor approval rules.

#### Scenario: Estimate exceeds the hard bound
- **WHEN** an ordinary research dry_run=false preflight exceeds USD 0.50
- **THEN** the adapter returns BUDGET_EXCEEDED without submitting paid work.

#### Scenario: Typed benchmark advancement
- **WHEN** a matching schema-valid benchmark proposal identifies its job family, strategy and budget category
- **THEN** the adapter enforces that category's configured cap and advances through the same recursive cycle so the producer retains job/result lineage.

#### Scenario: Existing compute approval job
- **WHEN** preflight returns an existing job awaiting approval
- **THEN** the adapter returns the retained evidence without attempting another paid submission.

### Requirement: Preserve horizon evidence
The existing decision evaluation adapter SHALL preserve producer horizon objectives, contract provenance, replay and control status without generating market or neural attribution claims.

#### Scenario: Partial evaluation returned
- **WHEN** the model returns partial or unavailable horizon evidence
- **THEN** the remote MCP returns that evidence unchanged for the hosted and direct client.
