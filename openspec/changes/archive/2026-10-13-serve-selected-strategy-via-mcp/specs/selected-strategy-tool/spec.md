# Spec Delta

## Purpose

Exposes the selected portfolio strategy to authenticated agents through a bounded and auditable MCP Lambda target.

## ADDED Requirements

### Requirement: Strategy-neutral read tool
The Gateway SHALL offer recommend_portfolio as a read-only tool using the producer's request and response contracts. It MUST call the selected strategy without exposing training, selection changes or order execution.

#### Scenario: Selected algorithm changes
- **WHEN** the producer pins another supported algorithm
- **THEN** the unchanged MCP tool returns that algorithm's recommendation

### Requirement: Five-minute target and compatible deadlines
The recommendation target SHALL permit up to 300 seconds. Its backend and caller deadlines MUST fit the request path. The computation MUST NOT depend on the existing 29-second job API integration.

#### Scenario: Slow valid computation
- **WHEN** the serving computation takes longer than 29 seconds but less than its backend deadline
- **THEN** the adapter can return its result through direct Lambda invocation

### Requirement: Scoped producer invocation
The adapter SHALL resolve the serving reference for its environment, invoke only that environment's FinanceModel service and retain the caller and correlation identifiers. It MUST reject cross-environment references and malformed producer responses.

#### Scenario: Wrong environment
- **WHEN** the serving reference names another environment
- **THEN** the adapter rejects it before invocation
