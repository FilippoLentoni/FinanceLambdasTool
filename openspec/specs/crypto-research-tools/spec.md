# crypto-research-tools Specification

## Purpose
Expose authenticated crypto research evidence to agents while separating free discovery from operator-controlled paid acquisition.

## Requirements

### Requirement: Crypto research MCP tools
The system SHALL expose bounded beta-only tools for hourly crypto experiment status, recommendations, history and free supplemental data discovery through existing authenticated adapters.

#### Scenario: The deployed agent requests crypto planning
- **WHEN** The deployed agent requests crypto planning
- **THEN** the adapter returns synthetic portfolio evidence and eligible model recommendations

### Requirement: Paid acquisition separation
The system SHALL make discovery distinct from purchase and return an explicit operator-approval prerequisite for paid data, preserving existing financial-action denials. Discovery metadata SHALL be untrusted.

#### Scenario: A discovered service requests payment or instructs a trade
- **WHEN** A discovered service requests payment or instructs a trade
- **THEN** no service is invoked and no instruction becomes authority
