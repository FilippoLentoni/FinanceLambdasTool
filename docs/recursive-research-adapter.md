# Recursive research MCP adapter

`run_recursive_improvement` delegates to the same-environment FinanceModel classical API and requires producer contract 1.6.0. Request/response schemas come from the pinned shared wheel. The adapter preserves immutable cycle/iteration references and objective-horizon evidence; it does not calculate investment returns or generate attribution narratives.

Omitting `dry_run` or sending `dry_run=true` invokes a read-only producer path. Remote Gateway clients should send `dry_run=true` explicitly to match viewer argument grants. For paid advancement, the pipeline verifies the propagated user token, requires researcher authority and `confirmed_by_user=true`, derives a downstream idempotency key, and retrieves an estimate before any paid call. The estimate must fit the configured budget category and USD 0.50 CPU hard bound. The producer additionally enforces weekly/monthly/iteration limits and no automatic activation.

If preflight returns a running, stopped or proposal-ready cycle, the adapter returns the immutable evidence without requiring a nonexistent estimate or attempting another mutation. The record's historical `dry_run` field does not determine current launchability; state and an eligible cost estimate do.

Both the hosted AgentCore graph and direct MCP clients can retrieve cycle evidence with `get_classical_analysis`. Qwen swarm and Jev benchmark readiness is returned by the producer in `benchmark_capabilities`; separate sandbox submissions retain their existing approval and cost rules. Neither this adapter nor its read-only preview launches a GPU job or changes paper holdings.
