# selected-strategy-tool Specification

## Purpose
Exposes the selected portfolio strategy to authenticated agents through a bounded and auditable MCP Lambda target.

## Requirements

### Requirement: PPO recommendation tool over an explicit universe
The Gateway SHALL offer generate_portfolio_recommendation_ppo as a read-only tool using the producer's request and response contracts. Its request MUST name the `instruments` universe; the adapter SHALL forward it unchanged to the FinanceModel serving function, which selects the frozen PPO policy trained on exactly that universe. It MUST NOT expose training, selection changes or order execution, and recommend_portfolio SHALL NOT be offered.

#### Scenario: Universe with a frozen policy
- **WHEN** the tool is called with the instruments of a frozen PPO policy
- **THEN** the MCP response returns that policy's targets only for those instruments

#### Scenario: Universe without a policy
- **WHEN** no frozen PPO policy was trained on the requested instruments
- **THEN** the producer's typed error is returned and no other universe's weights are served

#### Scenario: Response outside the universe
- **WHEN** the producer returns a target weight for an instrument not requested
- **THEN** the adapter fails the call instead of returning it

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
