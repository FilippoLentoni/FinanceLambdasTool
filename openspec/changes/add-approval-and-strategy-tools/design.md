# Design

## Context

See proposal.md (Why). `add-mcp-tool-adapters` already provides the common request pipeline, the three role classes (`reader`, `submitter`, `plan-writer`), the catalog and the release publication, and they are used unchanged. FinanceModel owns the selection operations and the SSM key. Tools run only in the deployed stack (decision 16).

## Goals / Non-Goals

**Goals:** one small tool so the user, through the agent or a direct client, can see and change the production strategy.

**Non-Goals:** approval tools (existing `publish_plan_version`), experiment or report tools (existing), and job submission.

## Decisions

### T1. One tool with an action field
A single `production_strategy` tool with `action: get | set | clear` keeps the catalog small, and the schema is a discriminated union in contracts 1.1.0. Separate tools were rejected as needless surface for one setting.

### T2. Role class per action
The Lambda runs under the `plan-writer` role class, but the handler routes `get` through read-only FinanceModel permissions. `set` and `clear` additionally require the `plan_publisher` group from the Gateway-supplied identity. A group check in the tool, on top of the FinanceAgent Gateway policy, gives defense in depth.

### T3. Confirmation flag
`confirmed_by_user` is required by the tool schema and forwarded to FinanceModel, which also enforces it. The agent sets it only after the user's explicit confirmation turn (FinanceAgent `agent-runtime-hosting` confirmation rule).

## Risks / Trade-offs

- [The LLM sets `confirmed_by_user` without asking] → the Gateway confirmation rule in FinanceAgent, the group check, and the audit event in FinanceModel. Clearing is always possible.
