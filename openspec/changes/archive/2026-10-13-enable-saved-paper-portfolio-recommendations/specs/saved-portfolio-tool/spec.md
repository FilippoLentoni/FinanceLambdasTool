# Spec Delta

## Purpose

Provide complete policy recommendations using the saved paper portfolio without repeatedly asking users for holdings.

## ADDED Requirements

### Requirement: Saved portfolio tool request
The recommend_portfolio MCP tool SHALL accept an empty request and forward it unchanged to the same-environment FinanceModel strategy service to resolve the saved paper portfolio and approved market snapshot. Existing explicit-state requests SHALL remain supported.

#### Scenario: Empty request
- **WHEN** an authenticated caller invokes recommend_portfolio with {} against a compatible producer
- **THEN** the adapter forwards {} once without initializing a portfolio or submitting jobs

### Requirement: Complete producer recommendation forwarding
The tool SHALL return validated complete producer allocation, cash, share-delta, saved-state and provenance evidence. It SHALL retain read-only permissions and reject incompatible producer releases before invoking inference.

#### Scenario: Share and cash evidence
- **WHEN** the producer returns instrument share changes and a cash target
- **THEN** the MCP response preserves those fields without writing holdings or altering strategy selection
