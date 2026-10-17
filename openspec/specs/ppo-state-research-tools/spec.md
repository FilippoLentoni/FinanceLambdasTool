# ppo-state-research-tools Specification

## Purpose

Expose producer-declared state research readiness and matched ablations through existing recursive research/experiment tools; retain approval, budget and same-environment validation.

## Requirements

### Requirement: State-research capability parity

MCP tools SHALL expose producer-approved state experiment profiles and coverage/readiness and retain exact experiment provenance, numerical results and limitations.

#### Scenario: A client requests an unsupported profile

- **WHEN** a caller supplies an arbitrary state configuration
- **THEN** the request SHALL be rejected rather than transformed into an authorized released experiment

### Requirement: Preserved research controls

State research SHALL retain same-environment resolution, verified identity, dry-run default, cost bounds and existing confirmation/approval requirements. News context SHALL NOT be represented as eligible historical training coverage.

#### Scenario: A client previews a comparison

- **WHEN** a dry-run state request is submitted
- **THEN** it SHALL show actual readiness and cost without starting training

