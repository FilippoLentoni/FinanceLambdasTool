# Proposal

## Why

The daily loop (decisions 18–22, 2026-10-08) produces recommendations that wait for the user's explicit approval. Experiments run only when the user asks, and the production strategy is the user's choice. The agent and direct MCP clients need tools for each of these actions, and the tools must be validated server-side so that an LLM or a malformed call cannot publish, select a strategy or spend money without the user's confirmed intent. The existing tools (`add-mcp-tool-adapters`) cover generic plans and experiments but have no review, strategy or benchmark-report operations.

## What Changes

- **Recommendation review tools:**
  - `list_pending_recommendations` (reader): pending model-run versions for the research plan, with the currently published version, lineage and `bias_disclosures`.
  - `approve_recommendation` (plan-writer): calls the platform publish route with the contract `approval` block. It requires `confirmed_by_user: true`, the expected checksum and the publication `expected_revision`.
  - `reject_recommendation` (plan-writer): calls the platform review route with a reason.
- **Strategy tools:**
  - `get_production_strategy` (reader).
  - `set_production_strategy` and `clear_production_strategy` (plan-writer). They call the FinanceModel selection operations and require `confirmed_by_user: true`.
  - Registry validation is FinanceModel's; tool errors pass through.
- **Benchmark tools:**
  - `run_benchmark` (submitter): submits a FinanceModel `benchmark` job over the universe on the latest approved snapshot, `dry_run` first by default.
  - `list_benchmark_reports` and `get_benchmark_report` (reader): by `report_id` or `latest`. They return a compact summary, the bias disclosures and a trusted reference.
- **Server-side validation on every new tool:**
  - pinned contracts 1.1.0 schemas;
  - Gateway-supplied caller identity and Cognito group:
    - `plan_publisher` for approve, reject and strategy changes;
    - `researcher` for `run_benchmark`;
    - any authenticated group for reads;
  - same-environment wiring, identifier formats and derived idempotency keys;
  - per-call `cpu_research` limit (USD 1).
- **Never trades.** No new tool records executions or accepts `live` mode, and approval only publishes. Tool roles stay denied execution routes, FinanceModel `approve_run`, and direct SSM writes.
- **Out of scope:** scheduling anything, approving paid compute jobs, accepting staged outputs, and editing risk preferences.

## Capabilities

### New Capabilities

- `recommendation-review-tools`: list, approve and reject pending model-run recommendations through the platform API, with explicit user confirmation and group checks.
- `strategy-selection-tools`: read, set and clear the per-environment production strategy through FinanceModel, with confirmation and pass-through of registry validation.
- `benchmark-tools`: on-demand benchmark submission with dry-run and budget limits, and benchmark report list and read through FinanceModel's stable report operations.

### Modified Capabilities

None. `openspec/specs/` is empty. The common request pipeline, role classes and release publication from `add-mcp-tool-adapters` apply unchanged to the new tools.

## Impact

- **Code (future):** eight new handler modules on the shared adapter library, catalog entries, and role-class grant updates (plan-writer gains the platform review route and the FinanceModel selection operations; submitter gains the `benchmark` kind; reader gains report reads). The repo pins contracts 1.1.0.
- **Producers:** FinancialPlanning `add-research-universe-and-daily-loop` (review route, approval block, `review_state` filter) and FinanceModel `add-daily-recommendation-and-on-demand-experiments` (selection and report operations, `benchmark` kind). Each tool returns `DEPENDENCY_UNAVAILABLE` until its producer release is present in the environment.
- **FinanceAgent:** registers the new tools as Gateway targets from the catalog (change `add-recommendation-review-flow`).
- **Cost:** Lambda only. `run_benchmark` jobs are at most about USD 0.12 each, charged to `cpu_research` under FinanceModel's authoritative checks.
