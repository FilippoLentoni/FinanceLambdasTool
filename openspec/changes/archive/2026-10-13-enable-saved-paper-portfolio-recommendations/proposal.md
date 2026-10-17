# Proposal

## Why

The existing recommendation tool requires caller state despite a user-approved saved paper portfolio. It must forward an empty default request and preserve the producer share deltas without adding portfolio mutations.

## What Changes

- Pin contracts 1.3.0 for default saved-paper requests and richer recommendation evidence.
- Describe the existing recommend_portfolio tool default and supplied-state modes.
- Preserve direct same-environment invocation, full producer response, read-only permissions and deadlines.
- Test empty requests and structured share/cash evidence forwarding.

## Capabilities

### New Capabilities

- `saved-portfolio-tool`: Saved paper portfolio recommendation behavior and evidence.

### Modified Capabilities

None. Existing capabilities are represented by unarchived changes.

## Impact

Existing recommendation contracts, runtime/tool code, skills and offline regression tests. Beta deployment only; no training, execution or new recurring costs.
