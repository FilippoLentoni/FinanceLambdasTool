# Traditional portfolio MCP adapters

Contracts 1.4.0 adds an independent traditional optimizer pathway. These tools call the same-environment FinanceModel classical Lambda, discovered at `/finplan/<env>/financemodel/api/classical-function-ref`; they never invoke the selected PPO artifact.

| Tool | Inputs and purpose |
|---|---|
| `recommend_classical_portfolio` | `algorithm` (`min_variance`, `mean_variance`, `cvar`), optional `settings`; defaults to saved paper book and latest approved completed snapshot. Historical scenarios supply `input_snapshot_id`, `as_of`, and complete `holdings` instead of `portfolio_id`. |
| `explain_classical_recommendation` | Issued `analysis_id`, optional `instrument_id`; reproduce objective-versus-keep and grouped Shapley evidence. |
| `compare_classical_plans` | `previous_analysis_id`, `current_analysis_id`; attribute changes between immutable decisions. |
| `evaluate_classical_performance` | `analysis_id`, optional `observed_snapshot_id`, `end_date`; distinguish paper allocation paths from unavailable broker execution and forecast evidence. |
| `get_classical_analysis` | Retrieve exact stored evidence by `analysis_id`. |
| `list_classical_analyses` | Optional `portfolio_id`, `kind`, `limit`; discover issued records. |
| `research_market_events` | `analysis_id`, optional `start_date`, `end_date`, `query`; dated public sources as context, never proven market causality. |
| `research_portfolio_models` | Optional `query`, `feedback`; sourced review and bounded experiment proposals, no paid launch. |
| `submit_portfolio_feedback` | `analysis_id`, `text`, `idempotency_key`; immutable caller-attributed audit feedback, without changing investment state. |
| `run_portfolio_research` | `review_id`, optional `dry_run` (defaults true). Paid calls require a verified researcher or configured direct-test owner, explicit `confirmed_by_user: true`, and `idempotency_key`. CI can estimate only. |

Each issued record has a `ca_<32 lowercase hex>` identifier, its kind, timestamp, summary, and a trusted `classical_analysis` artifact reference/checksum. Recommendations include the completed decision date, input snapshot/checksum, portfolio state, settings checksum, full allocation and fractional share deltas. There are no fabricated PPO run/export identifiers. Analysis reads can persist immutable audit evidence; they do not execute trades or change saved holdings.

Requests are closed schemas with bounded settings and text. Storage paths and arbitrary endpoints are rejected. The adapter preserves producer errors and numeric evidence, validates outputs and reference identity, bounds responses to 64 KiB, and permits public HTTP(S) citations only in the `sources` collection. Model outputs outside those citation pointers are still scanned for storage/account leaks.

Paid research first obtains a producer dry-run estimate, enforces the configured category limit plus a USD 0.50 hard cap, and then forwards the same caller-derived idempotency key. FinanceModel remains authoritative for weekly frequency, active jobs and project/category budgets; no tool can approve a job, activate a new strategy or execute a trade. The tool Lambda IAM roles can invoke only the exact classical serving function in their own environment; the reader's existing PPO serving grant is retained.

Paid MCP research also verifies the transport-only `X-Finplan-User-Token` propagated by AgentCore into Lambda client context. Its signing key comes only from the same beta Cognito pool's pinned SSM issuer/pool reference, with RS256 signature, issuer, allowed client, access-token type and expiration/not-before checks. Only signed `researcher` groups can launch paid work; CI credentials cannot. Tokens never enter tool arguments, producer requests, feedback, logs or model context. Direct configured owner tests retain their existing authorization. Other Gateway tools continue to use the honest aggregate gateway identity, and dry runs need no extra token verification.

The public MCP name `explain_classical_recommendation` uses physical Lambda suffix `explain-classical-plan` so all environment names fit AWS's 64-character function-name limit. The SSM tool-reference name and catalog schema IDs retain the full public tool name.
