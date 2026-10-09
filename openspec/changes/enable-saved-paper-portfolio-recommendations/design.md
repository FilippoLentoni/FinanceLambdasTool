# Design

## Context

See proposal.md for the user-visible gap. Existing recommendation handling uses the selected strategy and an authenticated read-only MCP target.

## Goals / Non-Goals

Serve the user-approved persisted paper portfolio automatically. Live execution, retraining, strategy promotion and broader discrepancy workers are outside this change.

## Decisions

Keep the existing target and direct strategy Lambda invocation. Contract 1.3 validates {} default requests and optional explicit supplied state. FinanceModel alone resolves and values saved holdings; the adapter does not initialize or mutate them. Gate recommendations on producer 1.3.0 to avoid advertising unsupported default semantics. Retain exact Lambda allowlist, no automatic invocation retry and existing response-size/deadline controls. Roll out Platform then Model then Tools then Agent in beta; no gamma/prod workloads or new target are required.

## Risks / Trade-offs

- Paper positions may differ from actual holdings → label the state source and retain explicit supplied-state mode.
- Recommendations could be mistaken for executions → proposals do not write holdings and the narrative states that boundary.
- Current prices could be stale → surface the producer completed session and decision timestamp.

## Contract compatibility

The original 1.2 `tools/recommend-portfolio-request` remains explicit-only. The 1.3 release adds `tools/recommend-portfolio-invocation-request` for the same MCP tool's saved-book/default mode and explicit mode. Gateway and tool input validation use the new invocation schema; the old schema stays unchanged.
