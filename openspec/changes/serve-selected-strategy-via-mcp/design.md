# Design

## Context

See proposal.md. Current local edits were started before OpenSpec tracking and are uncommitted. The current MCP adapter reaches FinanceModel through a short job API; the user clarified a direct, five-minute Lambda target on 2026-10-09.

## Goals / Non-Goals

**Goals:** one selected-strategy recommendation interface with bounded request latency, exact model semantics and provenance.

**Non-Goals:** training during a recommendation, automatic promotion, broker execution, and changes to full explanation evidence semantics.

## Decisions

The recommend_portfolio target has a 300-second Lambda timeout; its direct FinanceModel invocation has a shorter deadline (280 seconds) and no automatic retries. Resolve api/strategy-function-ref through same-environment SSM and validate the ARN before invocation. The reader role gets InvokeFunction for that function only; it still cannot submit/approve training or change selection. The inner serving Lambda caps at 270 seconds.

The selected research policy remains beta advisory/paper while the existing promotion gate is unmet. This is separate from the production-strategy key. The source run and explicit strategy selector freeze the chosen parameters; no fallback to a different algorithm is silent. Unsupported algorithms fail explicitly.

## Risks / Trade-offs

- [Cold starts or data reads dominate inference] → use bounded inputs, nested deadlines and a deployed latency check; no always-on endpoint.
- [Different preprocessing changes the policy] → compare exported deterministic actions with the training library and constrain the frozen universe and feature order.
- [Read requests still incur cost] → no training privileges, fixed timeouts, bounded request size and beta validation under the round's USD 2 cap within the USD 50 project budget.

## Migration Plan

Release producer contracts, model service, adapters, then agent into beta through their existing pipelines. Preserve gamma/prod gates. Roll back to the recorded releases or clear the advisory pointer; source artifacts remain immutable. Existing uncompleted explanation tasks stay uncompleted until their specified deployed tests pass.
