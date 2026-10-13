# Proposal

## Why

On 2026-10-09 the user clarified that the agent must invoke whichever portfolio strategy is selected after offline experimentation through an MCP Lambda target. The existing research pipeline trains policies but does not provide this strategy-neutral on-demand serving path.

## What Changes

- Expose recommend_portfolio as a read-only MCP Lambda target with a 300-second timeout.
- Invoke the FinanceModel serving Lambda directly, resolving its reference in the same environment; avoid API Gateway for this computation.
- Keep strategy code and data ownership in FinanceModel and preserve caller audit, contract validation and environment isolation.

## Capabilities

### New Capabilities

- `selected-strategy-tool`: this repository's part of reproducible, on-demand selected-strategy recommendations.

### Modified Capabilities

None in the archived spec inventory. This complements the existing in-flight daily and explanation changes; it does not replace performance replay with an allocation-hold approximation.

## Impact

Adapter transport, tool registry, timeout configuration and reader-role IAM. No direct raw research-store access or training privilege. Validation starts offline. Any new AWS work in this round stays below USD 2 and within the user's USD 50 project budget; no fresh training is authorized by an inference request.
