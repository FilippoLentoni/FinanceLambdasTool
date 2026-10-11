# Spec Delta

## Purpose

Provide reproducible classical portfolio tools for auditable beta portfolio decisions, explanations and controlled research.

## ADDED Requirements

### Requirement: Classical MCP adapters
The tools SHALL validate strict requests, call the same-environment classical backend and preserve validated numerical results, trusted references and producer errors. They MUST NOT invoke the PPO policy for a classical request.

#### Scenario: Traditional call
- **WHEN** recommend_classical_portfolio is invoked
- **THEN** the classical backend operation receives the exact validated arguments and its result is returned.

### Requirement: Research authorization
Paid sandbox submissions SHALL require an authorized submitter, explicit confirmation and idempotency. Readers and CI MUST be restricted to research dry-runs; feedback audit writes MUST NOT modify investment state.

#### Scenario: CI paid request
- **WHEN** CI requests a paid run
- **THEN** the request is denied and no backend job launches.

### Requirement: Published tool catalog
Release resolution SHALL publish the classical tool Lambda aliases, schemas, role classes and same-environment producer compatibility for registration in the separate MCP gateway.

#### Scenario: Beta registration
- **WHEN** the new gateway resolves beta tools
- **THEN** all targets name beta aliases and no Gamma/prod endpoint is referenced.
