# Proposal

## Why

FinanceAgent's AgentCore Gateway, Claude Code/Codex (through the Gateway) and the project owner testing directly all need one small, safe set of tools to reach market data, experiments and plans. Those tools must never become a second source of truth or a back door around platform and model controls. FinanceLambdasTool is third in the integration order fixed by FinancialPlanning change `establish-cross-repo-contracts` (platform, then model service, then tool wrappers, then agent/Gateway). It has to release fixture-backed tools before any model compute exists so that FinanceAgent can register Gateway targets and run phase 1 end-to-end checks.

## What Changes

- Add MCP adapter Lambdas (one per tool) that are thin, stateless adapters over the FinancialPlanning plan/ingestion APIs and the FinanceModel job interface:
  - `describe_capabilities`: lists tools, the contract version served, the release ID, environment and dependency availability.
  - `refresh_market_data`: on-demand ingestion through the platform ingestion operation. The platform's phase 2 daily provider is `yfinance` (Yahoo Finance, unofficial, no API key, pinned version) behind the platform's provider adapter interface, with the `exchange_calendars` XNYS calendar. The tool never calls `yfinance` itself; it passes through the platform's rate-limit/backoff outcomes and quality flags for empty or partial responses.
  - `query_market_data`: snapshot metadata and observation summaries for an `input_snapshot_id`. The initial instrument is an S&P 500 tracking-ETF daily series (for example SPY), daily completed observations only. It reads only `approved` platform snapshots and surfaces their lineage (provider, provider library version, retrieval timestamp) and quality flags.
  - `submit_experiment`: asynchronous job submission to FinanceModel. Supports `dry_run` cost estimates.
  - `get_job_status` and `get_experiment_result`: status and compact result summaries.
  - Plan operations: `get_plan`, `get_plan_version`, `list_plan_versions`, `create_override_version` (a child version only), `validate_plan_version` and `publish_plan_version` (publishes an exact validated version through the platform API).
- Validate on the server side, before any downstream call: the pinned contract schemas, parameters, contract major, caller identity and invocation source, environment, snapshot/configuration compatibility, and tool budgets. Rejections use the contract error envelope and registered codes.
- Make tools idempotent. Each write tool requires an `idempotency_key`, which it maps deterministically onto downstream idempotency keys, so retries and duplicates never create a second run, version or publication.
- Report `completion_status` separately from `solution_status`, so that infeasible and no-effect outcomes are distinct from failures. Partial or incomplete outputs are flagged explicitly.
- Return compact, size-bounded summaries and only trusted artifact references or platform-issued download grants. Requests containing caller-chosen storage paths are rejected.
- Enforce per-call tool budget limits by budget category, derived from the USD 50 total AWS budget and its default allocation (each limit at most a fifth of its category: `cpu_research` USD 1, `gpu` USD 5, all other categories 0). FinanceModel's category checks and the USD 50 AWS Budgets deny stay authoritative.
- Wire each environment strictly from its own configuration. Beta, gamma and prod share one AWS account in us-east-2, isolated by naming, environment tags, IAM permission boundaries and separate per-environment resources. Gamma Lambdas resolve and call only gamma platform and gamma FinanceModel references. Tools whose producer release is absent in an environment return `DEPENDENCY_UNAVAILABLE`.
- Publish, per environment, each Lambda reference, a tool catalog (tool → input/output schema `$id`, contract version, release ID) and the release manifest under `/finplan/<env>/financelambdastool/...`. FinanceAgent uses them for Gateway registration.
- Allow direct Lambda invocation before Gateway exists by exactly one direct-test principal per environment, the project owner, referenced through SSM by name only and never by ARN (RESOLVED 2026-10-07, LT-OQ-5). An in-process mock job backend and the contract-package fixtures make local tests credential-free.
- **Phase 1 (this change's first deliverable):** tools backed by fixtures and synthetic portfolios. `submit_experiment` runs against the in-process mock backend in tests and against FinanceModel's CPU fixture stub job in deployed environments. No GPU compute, no live provider, no live trading.
- **Later phases:** real-provider ingestion (the platform's `yfinance` adapter in phase 2) and real experiment types are enabled by configuration and contract minors, with no tool redesign. No provider API key or secret is needed by the platform's provider or by any tool. Retrieved market data is never committed to this public repo; tests use the mock provider and synthetic fixtures only.
- **Out of scope:** any trade execution or execution-recording tool, live trading, Coinbase, AgentCore payments, wallet spending, approving paid jobs, and automated rewriting of risk preferences.

## Capabilities

### New Capabilities

- `tool-request-handling`: the common adapter pipeline every tool runs: contract-version and schema validation, identity and invocation-source checks, environment checks, idempotency-key derivation, error-envelope mapping, compact response bounds and the trusted-reference-only rule.
- `capability-discovery`: the `describe_capabilities` tool and its view of tools, contract versions, release, environment and dependency availability.
- `market-data-tools`: `refresh_market_data` and `query_market_data` over the platform ingestion and snapshot APIs.
- `experiment-tools`: `submit_experiment`, `get_job_status` and `get_experiment_result` over the FinanceModel job interface, including budget pre-checks, snapshot/configuration compatibility, async semantics and outcome reporting.
- `plan-tools`: read, list, override-as-child-version, validate and publish plan operations over the platform plan API, with no trade execution.
- `tool-environment-wiring`: per-environment resolution of producer references, dependency gating, invocation permissions (Gateway principal and the single project-owner direct-test principal) and isolation.
- `tool-release-publication`: Lambda references, tool catalog, release manifest and pipeline stages used for Gateway registration and promotion.

### Modified Capabilities

None. This repository has no existing specs.

## Impact

- **This repo:** new Python Lambda handlers, a shared adapter library, a mock job backend for tests, CDK stacks per environment and the repo pipeline. It owns no authoritative data store.
- **FinancialPlanning:** consumed through `/finplan/<env>/financialplanning/api/plan-endpoint` and `/finplan/<env>/financialplanning/api/ingestion-endpoint`. Some routes the tools need, such as listing plan versions and reading observations, are not yet in the platform design and are recorded as blockers.
- **FinanceModel:** consumed through `/finplan/<env>/financemodel/api/job-endpoint` (`submit_job`, `get_job_status`, `get_job_result`, `dry_run`). FinanceModel stays the only minter of `run_id` and the only authority on job budgets and approvals.
- **FinanceAgent:** reads `/finplan/<env>/financelambdastool/lambda/<tool>-arn`, the tool catalog and the release manifest to register Gateway targets.
- **Contracts:** pins `finplan-contracts` 1.x by exact version and digest. The gaps found in the first pass (G-1..G-6) are resolved; see design.md.
- **Bootstrap:** one-time, with the user's existing authenticated AWS CLI session; it reuses an existing GitHub CodeConnection referenced through SSM and verifies repo access with a source-stage dry run. The user approved the bootstrap in principle (2026-10-07); it runs only after the bootstrap IaC is implemented, shows the exact stacks and a cost estimate at run time, and nothing is deployed during spec work. None of this is a blocker.
- **Cost:** Lambdas run on demand with no VPC, NAT, provisioned concurrency or endpoints, so standing cost is near zero inside the `platform_infra` category (USD 8) of the USD 50 total. TypeSafe Jev usage is billed outside AWS and is not counted.
