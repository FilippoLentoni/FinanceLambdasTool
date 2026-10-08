# Design

## Context

See proposal.md (Why) for motivation, and `specs/` for requirements. FinanceLambdasTool is the third repo in the integration order. All shared rules come from FinancialPlanning change `establish-cross-repo-contracts` and are referenced, not redefined, here:

- identifier formats and minting authority: tools mint nothing;
- the `finplan-contracts` package, pinned by exact version and digest;
- the error envelope and its registered codes;
- `completion_status` versus `solution_status`;
- trusted artifact references;
- the SSM convention `/finplan/<env>/<repo>/<category>/<name>`;
- the release manifest;
- the pipeline standard (D6);
- isolation rules ENV-03 and ENV-05;
- the phase plan (D7).

Producer interfaces consumed (observed in sibling planning changes, not yet implemented):

- **FinancialPlanning `add-platform-foundation`:** an IAM/SigV4 REST plan API at `/finplan/<env>/financialplanning/api/plan-endpoint`. Its routes cover creating and reading versions, validate, publish, executions, snapshots and `POST /v1/ingestions` at `…/api/ingestion-endpoint`. In phase 1, ingestion is synchronous (its PQ-6). The project budget is USD 50 total for all AWS spend (user decision 2026-10-07) at `/finplan/shared/financialplanning/config/cost-ceiling-usd`, split by the default category allocation at `/finplan/shared/financialplanning/config/budget-allocation` (`platform_infra` 8, `cpu_research` 7, `bedrock_explanations` 5, `gpu` 25, `reserve` 5; configurable), with AWS Budgets alerts at 50/80/100%, a budget-action deny at 100% and a `budget-state` flag. TypeSafe Jev usage is billed by TypeSafe (prepaid credits), outside this budget. The initial market-data instrument is an S&P 500 tracking-ETF daily series (for example SPY) under the dataset `finance/etf-daily/<instrument>`, distinct from the index level and the constituent universe; daily completed observations only. Phase 1 uses the fixture provider. The phase 2 provider is `yfinance` (platform OQ-5 resolved 2026-10-07): Yahoo Finance via an unofficial library, no API key, pinned version, daily completed OHLCV plus adjusted close, dividends and splits, behind the platform's provider adapter interface (so a fallback can be added without contract changes), with the `exchange_calendars` XNYS calendar at a pinned version. It runs inside the platform's ingestion Lambda/container, may be rate-limited (the platform retries with backoff) and records empty or partial responses as quality flags. Snapshot lineage records provider, library version and retrieval timestamp. Snapshots carry `status` `committed`, `approved` or `expired`.
- **FinanceModel `add-research-job-foundation`:** an API Gateway SigV4 job API at `/finplan/<env>/financemodel/api/job-endpoint` with `submit_job` (`dry_run`, `purpose`), `get_job_status`, `get_job_result`, `cancel_job` and `list_jobs`. Phase 1 uses CPU fixture stub jobs, approval threshold 0, and `approve_run` denied to tool roles.

### Observed facts (discovery 2026-10-07)

- Region us-east-2. AgentCore responds there, and no Gateway or Runtime exists yet. No ECR repositories exist. The FinanceLambdasTool GitHub repo is public, empty, on `main`.
- One AWS account holds beta, gamma and prod (user decision 2026-10-07, contracts OQ-1 resolved). Isolation is by naming, environment tags, IAM permission boundaries with environment-tag denies, and separate per-environment resources (contracts D5). Multi-account is only a possible future migration and would change environment configuration only.
- Existing AVAILABLE GitHub CodeConnections in us-east-2 are reused; the repo is `FilippoLentoni/FinanceLambdasTool` (public, `main`). The connection is referenced through SSM (`/finplan/shared/financelambdastool/config/codeconnection-ref`), never by literal in repo files. Repo access is verified by a pipeline source-stage dry run at bootstrap; only if that fails does the user extend the GitHub App installation (contracts OQ-2, non-blocking).
- The one-time bootstrap runs with the user's existing authenticated AWS CLI session (contracts OQ-11 resolved, non-blocking). No pre-existing scoped human role is required and a root caller is not refused. The bootstrap still creates this repo's scoped pipeline, deploy and Lambda role-class roles; moving the human operator to a scoped/MFA role later is a recommendation, not a blocker.

### Assumptions (unverified)

- LA-1. Python 3.12 on arm64 Lambda, deployed with CDK (contracts A2 and A3).
- LA-2. AgentCore Gateway can target a Lambda function and supply tool-call context to it. The exact event and context shape, inbound identity propagation, payload size limit and invocation timeout are **not verified** (LT-OQ-1 and LT-OQ-2). The design isolates them behind one adapter (D3), so any answer changes one module only.
- LA-3. Lambda direct invocation does not expose the caller's IAM identity to the function. Trust therefore comes from the resource policy, plus a per-environment "direct-test" principal class.

## Goals / Non-Goals

**Goals:**

- Thin, stateless, independently testable adapters.
- One shared request pipeline, so validation, identity, idempotency and error rules are implemented once.
- Releasable in phase 1 before FinanceModel compute and before FinanceAgent's Gateway exists.

**Non-Goals:**

- Owning any data store, cache table, or idempotency table.
- Cancelling runs or approving paid jobs.
- Recording paper executions. That is a platform operation, not exposed as a tool in this change.
- Excel import tools.
- Gateway registration itself, which FinanceAgent owns.
- Any trading, payment or wallet integration.

## Decisions

### D1. One Lambda function per tool, one artifact, three role classes

Tools and the platform routes they call:

| Tool | Kind | Producer call | Role class |
|---|---|---|---|
| `describe_capabilities` | read | SSM/manifests only | `reader` |
| `query_market_data` | read | `GET /v1/snapshots/{id}`, `GET /v1/snapshots/{id}/observations` | `reader` |
| `get_plan` | read | `GET /v1/plans/{plan_id}` | `reader` |
| `get_plan_version` | read | `GET /v1/plan-versions/{id}` | `reader` |
| `list_plan_versions` | read | `GET /v1/plans/{plan_id}/versions` | `reader` |
| `get_job_status` | read | `get_job_status` | `reader` |
| `get_experiment_result` | read | `get_job_result` | `reader` |
| `refresh_market_data` | write | `POST /v1/ingestions` | `submitter` |
| `submit_experiment` | write | snapshot read, `submit_job` dry run, then `submit_job` | `submitter` |
| `create_override_version` | write | `POST /v1/plans/{plan_id}/versions` | `plan-writer` |
| `validate_plan_version` | write | `POST /v1/plan-versions/{id}/validate` | `plan-writer` |
| `publish_plan_version` | write | `POST /v1/plans/{plan_id}/publications` | `plan-writer` |

- **One function per tool.** This matches the contract SSM key `/finplan/<env>/financelambdastool/lambda/<tool>-arn`. It gives per-tool invoke grants, metrics and timeouts, and it lets Gateway target each tool directly. A single router Lambda was rejected because one grant would cover every tool, including the write tools.
- **One build artifact.** All functions share one zip with a handler per tool, so the build produces a single digest (ENV-10).
- **Three IAM role classes.** `reader` has only the platform and job GET routes. `submitter` adds ingestion and job submit. `plan-writer` adds version create, validate and publish. All three carry the contract permission boundary (contracts D5), which denies any action on resources tagged with, or named under, another environment. All three also have an explicit deny on platform execution routes, job `approve_run` and `cancel_job`, any SageMaker, trading, payment or wallet action, and every other environment's resources (by name prefix and environment tag, since all environments share one account). Role classes, rather than one role per tool, keep the platform resource policy small while still separating reads from writes.
- **Published role references.** Role references are published at `/finplan/<env>/financelambdastool/lambda/role-<class>-arn`, so the platform resource policy can name them. The platform design reads `/finplan/<env>/financelambdastool/lambda/*`.
- **Function settings.** No VPC, no provisioned concurrency, arm64, 256 MB, and per-tool timeouts from configuration (read tools 15 s; `refresh_market_data` up to the platform sync ingestion bound). The Gateway timeout is unverified (LT-OQ-2).

### D2. Shared request pipeline (spec tool-request-handling)

Order inside every handler:

1. Resolve the invocation source to a caller identity (D3).
2. Check the contract major. Otherwise `UNSUPPORTED_CONTRACT_VERSION`.
3. Validate the input schema with the pinned package validator. Otherwise `VALIDATION_FAILED` or `INVALID_IDENTIFIER`.
4. Reject storage-like inputs (`s3://`, `arn:`, path-like) before any other work.
5. Check the environment.
6. Check tool-specific preconditions (D5, D6).
7. Call the producer through a SigV4 client.
8. Map producer errors one-to-one to the envelope. Unknown errors become `INTERNAL`.
9. Bound the response (D7).
10. Validate the output schema. If the output fails, return `INTERNAL`. A non-conformant response is never sent.
11. Write the audit log.

Producer references are read from SSM with an in-memory TTL cache (default 300 s). A rollback or rewire takes effect within that TTL.

### D3. Caller identity and invocation source

- **Gateway.** When the invocation carries Gateway context, the identity is `gateway:<env>`, plus the Gateway-supplied end-user identity if the Gateway propagates one (LT-OQ-1). Until that is verified, all Gateway callers in an environment share one principal class.
- **Direct test.** RESOLVED 2026-10-07 (LT-OQ-5, user decision 15b): each environment has exactly **one** direct-test principal, the **project owner**. It is referenced through SSM by IAM principal **name** only, at `/finplan/<env>/financelambdastool/config/direct-test-principal-name`; the parameter never holds an ARN, and no ARN, account ID or principal name appears in repository files. The value is written once per environment by the bootstrap from local untracked configuration. At deploy time the stack builds the invoke grant from that name in the deploying account. Validation fails the deploy if the value looks like an ARN (`arn:` prefix), contains a wildcard or an account ID, names the account root, or lists more than one principal. A missing parameter means no direct-test grant (fail closed).
  - Direct invocations from that principal carry a marker field. The identity is `direct:<env>`. Any `caller` value in the request body is logged as untrusted only. The marker is not a secret. Trust comes from the Lambda resource policy, which grants invoke only to the configured principal. The marker only selects the identity class.
  - In prod the project owner's direct-test grant covers the read-only tools only; state-changing tools are not directly invocable in prod.
  - The pipeline's own test and smoke roles (created by this repo's pipeline, not configured) are separate from the direct-test principal; they run the beta/gamma suites and the prod read-only smoke. No colleague principals exist for now; adding one is a later user decision.
- **Unknown source.** An invocation that matches neither source returns `UNAUTHORIZED`.
- **Caller block.** The resolved identity is forwarded to producers as the contracts `core/v1/caller.json` block in a transport header, outside the hashed body (contracts D10), so producer audit events record the end caller.
- **Downstream.** The platform and FinanceModel see the tool's role-class principal. They enforce their own authorization, such as FinanceModel denying `production_candidate` and approval for tool roles.

### D4. Idempotency without a tool-side store

- The tool forwards a derived downstream key: `lt_` plus the lowercase hex SHA-256 of `caller_identity | env | tool | idempotency_key`. That is 67 characters, inside the contract's 1–128 `[A-Za-z0-9_-]` range.
- Duplicate detection and `IDEMPOTENCY_KEY_REUSED` come from the producers' 7-day idempotency records.
- The downstream body must be a deterministic function of the tool request, so the producers' request hash is stable across retries. Timestamps and correlation IDs are kept in headers, never in the body.
- Rejected: a FinanceLambdasTool DynamoDB idempotency table. It would duplicate producer idempotency and add owned state against the "no authoritative storage" boundary.

### D5. `submit_experiment` flow and budgets

1. Validate the schema and domain payload, and compute `configuration_id` locally.
2. Reject purpose `production_candidate`.
3. Read the snapshot from the platform (exists in this environment, domain matches, coverage spans the window).
4. Call FinanceModel `submit_job` with `dry_run`. It returns an estimate (`estimated_usd_upper_bound`, `budget_category`) and `configuration_id`. A `configuration_id` mismatch returns `INTERNAL`.
5. Look up the per-call limit for the estimate's `budget_category` in `/finplan/<env>/financelambdastool/config/tool-limits` → `max_estimated_usd_per_call`. If the category is missing from the map, or the estimate exceeds its limit, return `BUDGET_EXCEEDED` with the estimate, category and limit in `details`.
6. Call `submit_job` with the derived key.

Budget enforcement (LT-OQ-3 resolved 2026-10-07):

- **One USD 50 total.** USD 50 is the total AWS budget for everything, not split per repo or per environment. FinanceLambdasTool owns no budget category of its own: its Lambdas are part of `platform_infra` (on-demand, near-zero standing cost, ENVW-07), and the experiments it submits spend FinanceModel's categories.
- **Per-call tool limits derived from the allocation.** `max_estimated_usd_per_call` is a map keyed by contract budget category. Each value is at most `per_call_max_fraction` (default 0.2) of that category's allocation in `/finplan/shared/financialplanning/config/budget-allocation`, so no single tool call can spend more than a fifth of a category. Defaults derived from the default allocation:

  | Category | Allocation (USD) | Default per-call tool limit (USD) | Rationale |
  |---|---|---|---|
  | `cpu_research` | 7 | 1.00 | At least seven tool-submitted CPU runs fit the category |
  | `gpu` | 25 | 5.00 | One GPU run per call stays a fifth of the category; FinanceModel still holds every GPU run in `awaiting_approval` until the user approves it with the cost estimate |
  | `platform_infra`, `bedrock_explanations`, `reserve` | 8, 5, 5 | 0 | Experiment tools never spend these categories |

  The fraction and values are configuration (`tool-limits`), not code. The pre-deploy check reads the allocation and fails the deploy if any configured per-call value exceeds `per_call_max_fraction` × its category allocation, or if a category named in `tool-limits` is not in the allocation. At run time the tool uses only the static `tool-limits` value, so step 5 stays deterministic for a given body.
- **Category and project budgets stay with FinanceModel and the platform.** FinanceModel's pre-flight check of the remaining category allocation is authoritative and its `BUDGET_EXCEEDED` is passed through. The account-level bound is the FinancialPlanning AWS Budgets deny action at 100% of USD 50, which also names this repo's `submitter` role (published in `/finplan/<env>/financelambdastool/config/budget-enforced-role-names`). The tool does not read `budget-state` itself.
- **Phase 1.** Only fixture job types run, with an estimate of 0, so every phase 1 submission passes the per-call check regardless of the limits.
- **Approval.** The tool never approves. `awaiting_approval` is a normal state; every GPU run needs explicit user approval with a cost estimate in FinanceModel.
- **TypeSafe Jev.** Jev-backed strategy runs are FinanceModel job types (enabled behind approval in phase 2). Their TypeSafe usage is billed outside AWS and is not part of the USD 50 or the per-call tool limits; only the AWS compute estimate of the driver job is checked. The tool never reads or forwards the Jev API key.

### D6. Plan and market-data specifics

- **Phase 1 synthetic-only guard.** Plan write tools read the portfolio's `synthetic` flag through the platform before writing. The platform enforces this too (it rejects non-synthetic portfolios in phase 1), so this is defense in depth.
- **No execution tool.** The tool catalog is checked at build time against a deny-list (execute, trade, order, payment, wallet, `execution`).
- **Initial instrument.** Examples, fixtures and conformance payloads for the market-data tools use the S&P 500 tracking-ETF daily dataset `finance/etf-daily/<instrument>` (for example SPY), never the index level `finance/index-level/<index>` or the constituent universe `finance/universe/<index>`. The ticker is configuration on the platform side, so the tools hard-code no ticker. Granularity is `daily` (completed observations) only; `intraday` is rejected as undeclared.
- **Market data.** `refresh_market_data` passes through the platform's declared provider capabilities, `BUDGET_EXCEEDED`, and the platform's provider-throttling outcomes (`RATE_LIMITED`/`DEPENDENCY_UNAVAILABLE` with their `retryable` flag); it adds no provider retries of its own, since the platform ingestion already backs off. `query_market_data` returns observations only from `approved` snapshots (otherwise `PRECONDITION_FAILED` with the status), returns coverage, quality flags (including those for empty or partial provider responses) and lineage (provider, provider library version, retrieval timestamp) exactly as the platform reports them, and sets `partial` when the requested range is not covered.
- **No provider in the tools.** Tools never import or call `yfinance`, `exchange_calendars` or any provider library, and read no provider secret (none exists). A build check fails on any such import in the source or artifact.
- **No retrieved data in the repo.** This repo is public and Yahoo's terms are personal/research use, so retrieved market data is never committed. Market-data fixtures are synthetic (`synthetic: true`, mock-provider lineage) and a build check rejects fixtures whose lineage names a real provider. CI uses the mock platform and mock provider only; this repo has no live-provider test (the optional rate-limited, shape-only live test belongs to the platform).

### D7. Compact responses

- **Size limit.** The byte limit comes from `tool-limits.response_max_bytes`. The default is 64 KiB, a conservative placeholder until the Gateway and MCP limits are verified (LT-OQ-2).
- **Truncation.** Lists are paginated with opaque tokens. The token wraps the producer's token and is not a storage pointer. Allocation summaries keep the top N weights plus an "other" bucket and the full content's trusted reference and checksum.
- **Metrics.** Experiment summaries keep FinanceModel's sections separate: portfolio performance, model accuracy, compute cost.
- **Strategy comparison (beta finding, 2026-10-08).** Beta showed `get_experiment_result` for a `run_benchmark` exposing only the optimizer's metrics; the controls, weights and units sat in a research artifact callers cannot read. FinanceModel now carries `payload.benchmark` (an extra key of the open 1.1.0 run-result payload; no contract change). The tool passes it through and adds a compact `comparison`: `rows` (strategy, role, `total_return`, `cagr`, `ann_volatility`, `sharpe`, `max_drawdown`, `turnover`, `transaction_cost`, `transaction_cost_fraction`), the primary (optimizer) strategy's final and average weights with cash, the evaluation window, risk-free assumption, base currency, initial capital and the unit legend. Only reviewed fields are copied (safe names, finite numbers). Over the byte limit the drop order is: candidate allocation, the full `payload.benchmark`, then the primary weights cut to `summary_top_n` plus `other`; the table always stays.

### D8. Phase 1 backends

- **Deployed environments** use only real producer endpoints in the same environment. FinanceModel's phase 1 interface serves CPU fixture stub job types. This keeps `run_id` minting with FinanceModel.
- **Market-data provenance (decision 26, data parity).** Platform market data in a deployed environment is whatever that environment's platform ingested: real phase 2 `yfinance` snapshots in beta and gamma, real or (transitionally) synthetic in prod. Nothing in the tools or the deployed suites assumes gamma or prod data is synthetic; `tests/deployed_support.check_snapshot_provenance` accepts both and checks the provenance is surfaced unchanged. Plan records created by tests stay synthetic; synthetic market-data fixtures are for offline/unit tests only.
- **Local tests and the build stage** use an in-process mock platform and a mock job backend driven by contract-package fixtures. These cover duplicate, conflict, infeasible, no-effect, partial-output, budget-rejection and schema-upgrade cases. The mocks live in a test-only package excluded from the deployable artifact (build check).
- **Rejected:** a deployed "fixture mode" in beta that fakes `run_id`s. It would mint identifiers outside FinanceModel and give false end-to-end signals.

### D9. Configuration and published outputs

**Read by this repo:**

| Key | Purpose |
|---|---|
| `/finplan/<env>/financialplanning/api/plan-endpoint`, `…/api/ingestion-endpoint` | Producers |
| `/finplan/<env>/financialplanning/release/manifest` | Served contract majors |
| `/finplan/<env>/financemodel/api/job-endpoint`, `/finplan/<env>/financemodel/release/manifest` | Producer and availability |
| `/finplan/<env>/financeagent/agent/gateway-principal-ref` | Gateway invoke grant (registered in contracts D4). Optional at deploy |
| `/finplan/<env>/financelambdastool/config/direct-test-principal-name` | Name (never ARN) of the single direct-test principal, the project owner (LT-OQ-5). Written by the bootstrap from local untracked configuration. In prod the grant covers read-only tools only |
| `/finplan/<env>/financelambdastool/config/tool-limits` | `response_max_bytes`, `max_estimated_usd_per_call` (map by budget category, D5), `per_call_max_fraction`, page sizes, timeouts |
| `/finplan/shared/financialplanning/config/budget-allocation` | Category allocation, read only by the pre-deploy check that bounds the per-call limits (D5) |
| `/finplan/shared/financelambdastool/config/codeconnection-ref` | Pipeline source connection, written by the bootstrap |

**Written by this repo:**

- `/finplan/<env>/financelambdastool/lambda/<tool>-arn`, alias-qualified.
- `/finplan/<env>/financelambdastool/lambda/role-<class>-arn`.
- `/finplan/<env>/financelambdastool/contract/tool-catalog`.
- `/finplan/<env>/financelambdastool/release/manifest` and `…/release/current-release-id`.
- `/finplan/<env>/financelambdastool/config/budget-enforced-role-names`: the `submitter` role name, for the FinancialPlanning budget deny action at 100% (contracts D4).

**Publication details:**

- Each function has an alias `current` pointing at the version deployed for that `release_id`. A rollback moves the alias to the recorded version from the stored assembly.
- The values are resolved at deploy time. No literal ARN or account ID is in the repo; IaC uses placeholders such as `<account-id>` only in docs.

### D10. Pipeline

The pipeline follows contracts D6.

**Build stage** runs:

- unit tests;
- contract conformance with the mock backends in producer and consumer modes;
- the copied-`$id` check;
- the leak and live-permission scans;
- the tool-inventory deny-list;
- the cost check (no provisioned concurrency, NAT or endpoints);
- `cdk synth` and the artifact digest.

**Promotion checks:**

- **Pre-deploy, each environment:** the per-call limit bound check against the budget allocation (D5), and a compatibility check against the platform manifest. A missing or incompatible platform blocks the deploy. FinanceModel absence only marks the experiment tools unavailable.
- **Beta:** a direct-invocation conformance suite plus integration-beta tests.
- **Gamma:** the same suite plus isolation tests.
- **Prod:** read-only smoke against the synthetic prod portfolio; prod market data may be real or synthetic (decision 26).

## Risks / Trade-offs

- [Gateway identity propagation unknown, so all Gateway callers may share one idempotency scope] → Derived keys still include the tool and environment. Phase 1 traffic is synthetic. Revisit when LT-OQ-1 closes; the change is confined to D3.
- [Platform read routes (G-1, G-2) ship in a later platform release than the write routes] → those tools report `DEPENDENCY_UNAVAILABLE` until the platform release with the routes (platform task 4.8) exists in the environment.
- [Tool schemas land in contracts 1.0.0 (G-3, contracts D10) after this change starts] → handlers are built against 0.x beta schemas first. Drafts are proposed to FinancialPlanning, never committed here as `$id`-bearing schemas, because that would fail CS-01.
- [Dry-run estimate could change between retries if the price configuration changes] → The estimate is 0 for phase 1 fixture jobs, and FinanceModel's idempotency returns the original run for an identical downstream request.
- [SSM reference cache delays a rewire or rollback] → TTL is 300 s, configurable. Rollback runbooks wait one TTL before smoke tests.
- [The direct-test marker can be spoofed by any granted invoker] → It only selects the identity class. The invoke grant is the security boundary; only the project owner and the pipeline test/smoke roles are granted, and in prod only for read-only tools.
- [The project owner currently operates as the account root user (contracts OQ-11)] → root cannot be the direct-test principal; validation rejects it. The owner names a non-root IAM principal at bootstrap (for example the scoped/MFA operator role recommended by contracts OQ-11); until then no direct-test grant exists and only the pipeline suites run.
- [Single account: a misconfigured role could reach another environment's resources] → permission boundaries with environment-tag and name-prefix denies on every role class, same-environment SSM resolution only, IAM policy-simulation unit tests and the gamma isolation suite. Multi-account remains a possible future migration that changes configuration only.
- [Per-call limits could drift above the allocation after the user edits it] → the pre-deploy bound check fails the deploy; FinanceModel's category check and the USD 50 Budgets deny remain the authoritative caps.
- [Tools deploy before the Gateway principal exists] → The Gateway grant is added by redeploying the same release (configuration-only) after FinanceAgent publishes its principal.

## Migration Plan

1. Prerequisites: the bootstrap IaC implemented (the user approved the one-time bootstrap in principle on 2026-10-07; at run time the exact stacks and a cost estimate are shown, and the run proceeds under that approval; nothing is deployed during spec work); the user's existing authenticated AWS CLI session (no scoped human role required); contracts ≥ 1.0.0 published with the tool schemas (G-3); the platform phase 1 release in beta, including the shared budget allocation.
2. Bootstrap this repo's pipeline once (authenticated CDK, after the STS account and region checks against local untracked configuration). It writes the CodeConnection reference, creates the scoped pipeline, deploy and role-class roles with the permission boundary, writes default `tool-limits` (D5) unless present, and runs a source-stage dry run. If the dry run cannot read the repo, the user extends the GitHub App installation to this repo and reruns it. The bootstrap prints a recommendation to move the operator to a scoped/MFA role later.
3. Release phase 1 to beta.
   - Plan, market-data and `describe_capabilities` tools are live on platform fixtures.
   - The experiment tools report `DEPENDENCY_UNAVAILABLE` until the FinanceModel beta release exists.
   - After it exists, a configuration re-resolution enables them; no rebuild is needed.
4. The pipeline runs the direct-invocation suite in beta with its test role, and the project owner (the single direct-test principal) runs the documented direct commands. Then gamma, then approval, then prod (read-only smoke).
5. FinanceAgent registers Gateway targets from the tool catalog. The same release is redeployed per environment to add the Gateway invoke grant.
6. Rollback: run the pipeline with `rollback_to_release_id`. The aliases and SSM references move back, and the manifest records `rolled_back_from`. There is no data to roll back, because the tools are stateless.

## Open Questions

| ID | Question | Blocks | Resolved by | Interim |
|---|---|---|---|---|
| LT-OQ-1 | How AgentCore Gateway presents tool calls to a Lambda target (event and context shape, end-user identity propagation) | BLOCKER for Gateway-path identity (D3) and Gateway conformance tests, not for direct invocation | Read the current AgentCore Gateway documentation and run a beta spike when FinanceAgent creates the Gateway | Gateway callers share `gateway:<env>` |
| LT-OQ-2 | Gateway/MCP response size and Lambda invocation timeout limits | None (configuration) | Same documentation and spike | 64 KiB responses, 15 s reads |
| LT-OQ-3 | Per-call tool cost limit for non-fixture experiments | None | **RESOLVED 2026-10-07:** USD 50 total, default allocation 8/7/5/25/5 (contracts OQ-7). Per-call limits by budget category, each ≤ 0.2 × its allocation: `cpu_research` 1.00, `gpu` 5.00 (GPU runs still need user approval in FinanceModel), all other categories 0; configurable in `tool-limits`, bounded by the pre-deploy check (D5) | n/a |
| LT-OQ-4 | Whether `cancel_experiment` and paper-execution recording should become tools | None | User decision | Not exposed |
| LT-OQ-5 | Who the direct-test principals are per environment | None | **RESOLVED 2026-10-07:** one direct-test principal per environment, the project owner, referenced through SSM by name only (`/finplan/<env>/financelambdastool/config/direct-test-principal-name`), never by ARN; written by the bootstrap from local untracked configuration; no colleague principals for now (D3) | Pipeline test role only until the bootstrap writes the name |
| LT-OQ-6 | Initial instrument and provider (contracts OQ-5) for real `refresh_market_data` | None | **RESOLVED 2026-10-07:** instrument = S&P 500 tracking-ETF daily series (for example SPY), dataset `finance/etf-daily/<instrument>`, daily completed observations only, no intraday (D6). Provider = `yfinance` (unofficial, no API key, pinned version) behind the platform's provider adapter interface, with `exchange_calendars` XNYS (platform OQ-5/PQ-5). Tools never call it; they pass through platform outcomes, quality flags and lineage (D6). No provider secret is needed | Phase 1: fixture dataset under the ETF identity and mock provider |
| (contracts OQ-1, OQ-2, OQ-11) | Account topology, CodeConnection coverage, bootstrap identity | None | **RESOLVED 2026-10-07:** single account in us-east-2 with per-environment isolation; reuse an existing AVAILABLE CodeConnection via SSM, verified by a source-stage dry run; bootstrap with the user's authenticated CLI session (Context) | n/a |
| (bootstrap approval) | Whether the one-time pipeline bootstrap may run | None | **RESOLVED 2026-10-07:** approved in principle. Runs only after the bootstrap IaC (task 9.6) is implemented; the exact stacks and a cost estimate are shown at run time; no deploy during spec work | n/a |

### Gaps against shared contracts and producers (status after the cross-repo review, 2026-10-07)

- G-1, G-2: resolved in `add-platform-foundation` (P4 read routes `GET /v1/plans/{plan_id}`, `GET /v1/plans/{plan_id}/versions`, `GET /v1/snapshots/{id}/observations`, plus `GET /v1/portfolios/{id}` for the D6 synthetic guard; spec requirement "Plan head, version list and observation reads").
- G-3: resolved (tool catalog and per-tool schemas in contracts 1.0.0, D10).
- G-4: resolved; the name is `/finplan/<env>/financeagent/agent/gateway-principal-ref` (contracts D4), matching FinanceAgent.
- G-5: resolved (proxied-call key derivation is now the contract rule; caller block, contracts D10).
- G-6: resolved (USD 50 total and category allocation recorded in contracts D11; `tool-limits` stays in this repo's namespace; per-call values decided in D5, LT-OQ-3).

Original wording:

- **G-1 (platform):** no route to read a plan head (`get_plan`) or list a plan's versions (`list_plan_versions`) in the `add-platform-foundation` route table.
- **G-2 (platform):** no observation read or summary route for `query_market_data`. Only snapshot metadata is exposed.
- **G-3 (CONTRACT GAP):** the contract coverage list has no per-tool MCP input/output schemas or tool-catalog schema, but the tool schemas are cross-repo (FinanceAgent registers them). Proposed: `core/v1/tool-catalog.json`, plus a tool request/response schema for each tool under `core/v1/tools/` (domain-neutral) and `finance/v1/tools/` (finance payloads).
- **G-4 (CONTRACT GAP):** no SSM name for the FinanceAgent Gateway invoke principal per environment. Proposed at the time: `/finplan/<env>/financeagent/agent/gateway-principal` (superseded by `gateway-principal-ref`).
- **G-5 (CONTRACT GAP):** idempotency is scoped to the "caller principal", but proxied calls arrive at the producers under the tool's role. The contract does not define on-behalf-of identity. D4's derived key is the interim answer.
- **G-6 (CONTRACT GAP):** the USD 50 ceiling is decided in the brief but contracts OQ-7 is still open, and there is no SSM key for per-repo tool limits. This design uses `/finplan/<env>/financelambdastool/config/tool-limits` within its own namespace.
