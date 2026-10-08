# Proposal

## Why

Choosing the production strategy that the daily recommendation job runs is a user action (decisions 20 and 22, 2026-10-08). The agent and direct MCP clients need one validated tool to read or change it. Approval is already covered by the existing `publish_plan_version`. Experiments and results are already covered by `submit_experiment`, `get_job_status` and `get_experiment_result` (`add-mcp-tool-adapters`). Neither is re-specified here.

## What Changes

- Add one tool, `production_strategy`, with `action`:
  - `get` (any authenticated caller) returns FinanceModel's current selection or `none`;
  - `set` (`strategy_id`, optional `model_version`) and `clear` require a Gateway-supplied caller in `plan_publisher`, `confirmed_by_user: true` and an `idempotency_key`.
- The tool calls only FinanceModel's production-strategy operations and passes registry validation errors through unchanged. It never writes SSM, never submits jobs and never trades.
- It is validated server-side through the existing request pipeline (contracts 1.1.0 schema, identity, environment, derived idempotency key), and is listed in the tool catalog.

## Capabilities

### New Capabilities

- `production-strategy-tool`: the single read/set/clear tool for the per-environment production strategy.

### Modified Capabilities

None. `openspec/specs/` is empty. Existing tools and the common pipeline are unchanged.

## Impact

- **Code (future):** one handler and its catalog entry. The `plan-writer` role gains FinanceModel's set/clear operations and the `reader` role gains get. The repo pins contracts 1.1.0.
- **Producer:** FinanceModel `add-daily-recommendation-and-on-demand-experiments`. The tool returns `DEPENDENCY_UNAVAILABLE` until that release is present in the environment.
- **Cost:** Lambda only, near zero.
