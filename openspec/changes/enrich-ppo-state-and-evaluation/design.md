# Design

## Context

See proposal.md. Four independently released repositories already provide beta portfolio advice and bounded research. Preserve the existing ownership boundaries and uncommitted archive work.

## Goals / Non-Goals

Goals: controlled state experiments, honest news eligibility, auditable results and the authorized ceiling. Non-goals: live trading, automatic strategy activation, paid GPU research or retrospective news fabrication.

## Decisions

Preserve existing tool names/contracts; open result payloads can expose producer metadata. Update projections and descriptions/packaged guidance as needed. Validate through dispatcher and beta remote MCP, including confirmed and denied paths. Reuse existing Lambda aliases and owner pipeline; refresh Gateway only if the catalog changes.

The user explicitly requested proposal followed by apply in this turn; that authorization overrides the generic propose skill planning-only pause. USD 100 is the total ceiling. Start with at most USD 5 incremental work and check billed/project-ledger headroom before each compute launch.

## Risks / Trade-offs

Inspected market path → development label and no promotion. Thin training history → report seed dispersion without equating seeds to independent market regimes. Missing news vintages → explicit ineligibility. Public repositories → synthetic fixtures only; private live receipts stay outside repos.

## Migration Plan

Validate locally, deploy owner artifacts through beta pipelines, preserve gamma/prod state and transition baselines, verify compatible consumer releases. Retain prior selected policy reference for rollback; no automatic activation.

### Large research evidence

Live state-preview verification on an accumulated paper book exceeded the 64 KiB Lambda
transport read cap. Bound producer transport at 1 MiB, validate all public identity/citation
and privacy fields before compaction, and return a compact research view below the unchanged
64 KiB MCP limit. Preserve exact proposal/approval arguments and immutable analysis references;
source analysis and experiment identifiers allow individual retrieval. Full producer artifacts
remain unchanged. Tests cover oversized histories, intact proposals and concealed private data.
