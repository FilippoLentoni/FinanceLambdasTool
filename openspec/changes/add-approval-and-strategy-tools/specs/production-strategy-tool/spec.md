# Spec Delta

## Purpose

Defines the single MCP tool that reads, sets or clears the environment's production strategy through FinanceModel, as a confirmed user action that is validated on the server side.

## ADDED Requirements

### Requirement: Read the production strategy
`production_strategy` with `action` `get` SHALL return FinanceModel's current selection document, or `none`, for the tool's own environment.

#### Scenario: Nothing selected
- **WHEN** no strategy is selected in beta
- **THEN** the tool returns `none` with a note that daily recommendations are off

### Requirement: Set and clear are confirmed user actions
`action` `set` (with `strategy_id`) and `clear` SHALL require `confirmed_by_user: true`, an `idempotency_key` and a Gateway-supplied caller in group `plan_publisher`. A request failing any check MUST be rejected before any FinanceModel call.

#### Scenario: Missing confirmation
- **WHEN** `set` is called without `confirmed_by_user`
- **THEN** it fails with `PRECONDITION_FAILED` `confirmation_required`, and FinanceModel receives no request

#### Scenario: Wrong group
- **WHEN** a `researcher`-only caller calls `set`
- **THEN** it fails with `FORBIDDEN`

### Requirement: FinanceModel decides validity
The tool SHALL call only FinanceModel's production-strategy operations with the on-behalf-of caller and a derived idempotency key. It MUST pass registry validation errors through unchanged.

#### Scenario: Registry rejection
- **WHEN** FinanceModel rejects the strategy with `no_evaluation_evidence`
- **THEN** the tool returns `VALIDATION_FAILED` with that detail

### Requirement: No side doors
The tool roles SHALL have no write access to any `/finplan/<env>/financemodel/config/*` parameter. The tool MUST NOT submit jobs, publish plans or record executions.

#### Scenario: Policy simulation
- **WHEN** the plan-writer role is simulated against `ssm:PutParameter` on the production-strategy key
- **THEN** the result is deny
