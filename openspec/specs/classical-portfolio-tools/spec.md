# classical-portfolio-tools Specification

## Purpose
Provide reproducible classical portfolio tools for auditable beta portfolio decisions, explanations and controlled research.

## Requirements

### Requirement: Classical MCP adapters
The tools SHALL validate strict requests, call the same-environment classical backend and preserve validated numerical results, trusted references and producer errors. They MUST NOT invoke the PPO policy for a classical request.

#### Scenario: Traditional call
- **WHEN** generate_portfolio_recommendation_min_variance or generate_portfolio_recommendation_cvar is invoked
- **THEN** the classical backend operation named by the tool receives the exact validated arguments, including the required instruments, and its result is returned.

### Requirement: Model fixed by tool and explicit universe
Each classical recommendation tool SHALL run exactly one model (min_variance or cvar) and accept no algorithm argument. Its required instruments SHALL be forwarded unchanged; a recommendation of another model or with a target weight outside the requested instruments MUST be refused. recommend_classical_portfolio SHALL NOT be offered.

#### Scenario: Weight outside the universe
- **WHEN** the producer returns a target weight for an instrument that was not requested
- **THEN** the tool fails the call instead of returning it

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
