# Tasks

Scope: phase 1 fixture-backed MCP adapter Lambdas. Tasks marked BLOCKED name the open question or gap in design.md that must close first. The user approved the one-time pipeline bootstrap in principle (2026-10-07): it runs only after the bootstrap IaC (9.6) is implemented, shows the exact stacks and a cost estimate at run time, and nothing is deployed during spec work. That approval is not a blocker. Contracts OQ-1, OQ-2 and OQ-11 are resolved (2026-10-07): single account, reused CodeConnection verified by a source-stage dry run, bootstrap with the user's existing authenticated CLI session; none of them blocks a task. No task adds trading, execution, payment or wallet functionality.

## 1. Repository scaffolding and contract pin

- [x] 1.1 Create the Python project layout (`src/financelambdastool/{core,tools,clients}`, `tests/{unit,contract,integration}`, `testing/mocks/` as a separate test-only package, `infra/` CDK app). Verify that `pytest` runs an empty suite and `cdk synth` produces an empty stack offline. Done locally 2026-10-08. Actual layout: `src/finplan_tools/{core,tools,backends}`, test-only package `testing/finplan_tools_testing/`, `tests/{unit,contract,integration,smoke}`; offline synth uses `LegacyStackSynthesizer` (L1).
- [ ] 1.2 Pin `finplan-contracts` by exact version and SHA-256 digest from the CodeArtifact reference in `/finplan/shared/financialplanning/contract/registry-ref`. Verify that a digest-mismatch fixture fails the install step (CS-04 consumer side). BLOCKED until contracts with the tool catalog and per-tool schemas (contracts D10) is published (0.x in beta, 1.0.0 for gamma and prod). PARTIAL 2026-10-08: pinned to the FinancialPlanning-built 1.0.0 wheel vendored at `vendor/finplan-contracts/` (version + SHA-256 in `contracts-pin.json`, pyproject and `uv.lock`; digest-mismatch, tampered-wheel, range-pin and 0.x-in-gamma/prod tests pass). Open: `/finplan/shared/financialplanning/contract/registry-ref` does not exist yet and 1.0.0 is not yet published immutably; re-pin with `uv run python scripts/check_contracts_pin.py --repin` once it is.
- [x] 1.3 Add the build-stage scans from the contract package: copied-`$id`, identifier and secret leak, live-financial permission. Verify CS-01, OWN-03 and ENV-05 with negative fixtures committed under `tests/fixtures/scan/`. Done locally 2026-10-08. Negative inputs are generated under `tmp_path` instead of committed under `tests/fixtures/scan/`, so this public repository holds no leak-shaped strings.
- [x] 1.4 Write `README.md`: purpose, ownership boundary (no authoritative storage, mints no IDs), the tool list, out-of-scope items, and how to run tests offline. Verify that the leak scan passes on the README

## 2. Shared request pipeline (spec tool-request-handling)

- [x] 2.1 Implement invocation-source resolution (Gateway context adapter stub, direct-test marker, unknown source → `UNAUTHORIZED`), keeping the Gateway parsing in one module (D3). Verify TRH-04 unit tests, including that a body `caller` field is ignored. The Gateway branch is BLOCKED for real event shapes by LT-OQ-1. Done locally 2026-10-08 (stub in `core/gateway.py`); real Gateway event shapes remain BLOCKED by LT-OQ-1 (task 10.4).
- [x] 2.2 Implement contract-major negotiation and input/output validation with the pinned validators. Verify TRH-01 and TRH-02 (major 2 → `UNSUPPORTED_CONTRACT_VERSION` with `served_majors`; a 1.0.0 request accepted by a 1.x pin)
- [x] 2.3 Implement identifier prefix checks and the storage-input rejection (`s3://`, `arn:`, path-like). Verify TRH-03 and TRH-09 (wrong prefix → `INVALID_IDENTIFIER`; `s3://` → `VALIDATION_FAILED` with no client call, asserted via mock call counts)
- [x] 2.4 Implement the environment check. Verify TRH-05 (gamma request to beta → `FORBIDDEN`, no client call)
- [x] 2.5 Implement derived idempotency keys (`lt_` + SHA-256 of identity|env|tool|key) and deterministic downstream bodies. Verify TRH-06 unit tests: same input gives the same key, two callers get different keys, the key matches the contract pattern, and the body is byte-identical across retries
- [x] 2.6 Implement error-envelope mapping: producer codes passed through, unknown → `INTERNAL`, throttling → `RATE_LIMITED`, unreachable → `DEPENDENCY_UNAVAILABLE`, `correlation_id` propagation. Verify TRH-07 and TRH-08 against every registered code fixture (CS-05)
- [x] 2.7 Implement response bounding (byte limit, pagination token wrapping, allocation top-N summary). Verify TRH-10 with an oversized fixture list (`truncated` true, `next_token` present, size ≤ limit)
- [x] 2.8 Implement the SSM reference resolver with an in-memory TTL cache, and confirm no mutable-head caching. Verify TRH-11 (a head change is visible on the next call) and the TTL unit test
- [x] 2.9 Implement the structured audit log. Verify TRH-12: the log record has the required fields, and a test asserts no download grant or payload appears

## 3. Mock backends and fixtures (test-only)

- [x] 3.1 Build the in-process mock platform (plan, version, publication, snapshot, ingestion) and the mock FinanceModel job API from contract fixtures, including idempotency and conflict semantics. Verify the mocks pass the contract producer-mode conformance suite (CS-10)
- [x] 3.2 Add fixture scenarios, using the S&P 500 tracking-ETF daily dataset (`finance/etf-daily/<instrument>`, for example SPY; daily completed observations only) for every market-data fixture: duplicate, key reuse, conflict, infeasible, no-effect, failed, timed-out partial, budget rejection per category (`cpu_research` over USD 1.00, `gpu` within USD 5.00 → `awaiting_approval`, zero-limit category), schema upgrade (1.0 → 1.x), unsupported major. Add market-data provider scenarios: platform `RATE_LIMITED` after backoff, a snapshot with quality flags for an empty/partial provider response, a `committed` (not approved) snapshot, and an approved snapshot with lineage (provider `yfinance`, library version, retrieval timestamp) whose observations are synthetic. Verify each scenario loads and validates (`synthetic: true`, mock-provider data); no fixture contains data retrieved from a real provider
- [x] 3.3 Add the build check that the deployable artifact excludes `testing/mocks`. Verify ENVW-05 negative test (an artifact containing the mock fails)

## 4. Capability discovery (spec capability-discovery)

- [x] 4.1 Implement `describe_capabilities` from the tool catalog, configuration and producer manifests. Verify CAP-01 and CAP-02 unit tests (model manifest absent → experiment tools `DEPENDENCY_UNAVAILABLE`; incompatible platform major → `UNSUPPORTED_CONTRACT_VERSION`)
- [x] 4.2 Report declared limits and granularities from configuration and producer declarations only. Verify CAP-03 (daily-only provider → no `intraday`; per-category budget limits reported from `tool-limits`) and CAP-04 (leak scan over the response)

## 5. Market-data tools (spec market-data-tools)

- [x] 5.1 Implement `refresh_market_data` over the platform ingestion operation. Verify MKT-01 (ETF daily fixture refresh), MKT-02 and MKT-04 with the mock platform: snapshot fields returned, a duplicate gives the same `input_snapshot_id` with one ingestion, and `BUDGET_EXCEEDED` passes through with no retry
- [x] 5.2 Pass through capability rejections. Verify MKT-03 (intraday against daily-only → `VALIDATION_FAILED`, no ingestion)
- [x] 5.2a Pass through provider throttling and incomplete-response outcomes from the platform ingestion (no tool-side provider retries). Verify MKT-08 with the mock platform (`RATE_LIMITED` retryable passed through with one ingestion call; partial-response quality flags returned unchanged with actual coverage)
- [x] 5.2b Add the build checks: no `yfinance`/`exchange_calendars`/provider-library import in source or artifact, no provider secret read, and market-data fixture provenance (`synthetic: true`, mock-provider lineage; a fixture naming a real provider fails). Verify MKT-10 (no provider import, no provider secret, no outbound provider call) and MKT-09 with negative fixtures
- [x] 5.3 Implement `query_market_data` over snapshot metadata and observation reads. Verify MKT-05 (ETF daily snapshot: `completed_daily` only, dataset reported as `finance/etf-daily/<instrument>`, never as index level or universe) and MKT-06 (partial coverage → `partial` true, no fabricated observations) and MKT-07 (unknown snapshot → `NOT_FOUND`), and MKT-11 (lineage: provider, provider library version, retrieval timestamp and quality flags surfaced unchanged; `committed`/`expired` snapshot → `PRECONDITION_FAILED` with status). The observation read uses platform `GET /v1/snapshots/{id}/observations` (G-2 resolved); in an environment whose platform release predates that route, return metadata only and report the observation summary as `DEPENDENCY_UNAVAILABLE`

## 6. Experiment tools (spec experiment-tools)

- [x] 6.1 Implement `submit_experiment` validation: domain payload, local `configuration_id`, purpose restriction, and snapshot compatibility through the platform. Verify EXP-03, EXP-04 and EXP-08 unit tests (coverage gap → `PRECONDITION_FAILED`; `production_candidate` → `FORBIDDEN`; mismatched `configuration_id` → `INTERNAL`)
- [x] 6.2 Implement the dry-run estimate and per-call tool limit check by `budget_category` (`tool-limits.max_estimated_usd_per_call` map; defaults `cpu_research` 1.00, `gpu` 5.00, others 0; missing category → reject). Verify EXP-05 (USD 1.40 `cpu_research` → `BUDGET_EXCEEDED` with estimate, category and limit in `details` and zero runs in the mock; zero-limit category rejected; USD 3.00 `gpu` submitted and reported `awaiting_approval`; fixture estimate 0 passes) and EXP-06 (dry run returns no `run_id`)
- [x] 6.2a Implement the pre-deploy per-call limit bound check (each limit ≤ `per_call_max_fraction` × category allocation from `/finplan/shared/financialplanning/config/budget-allocation`; unknown category fails). Verify EXP-15 with fixture allocations (defaults pass; `gpu` allocation 10 with limit 5.00 fails naming the bound 2.00)
- [x] 6.3 Implement async submission with the derived key and dependency gating. Verify EXP-01 and EXP-02 (immediate return with `queued`/`awaiting_approval`; FinanceModel manifest absent → `DEPENDENCY_UNAVAILABLE`) and the TRH-06 duplicate test (one mock run for two identical calls)
- [x] 6.4 Implement `get_job_status`. Verify EXP-09 (running state, timestamps, no storage locations) and EXP-07 (`awaiting_approval` reported; no tool code path calls approve, enforced by a static check on client methods)
- [x] 6.5 Implement `get_experiment_result` with separate completion and solution status, non-terminal handling, partial flags and compact sections. Verify EXP-10 (optimal, infeasible, failed), EXP-11 (queued → `PRECONDITION_FAILED`), EXP-12 (timed-out → `artifacts_complete` false), EXP-13 (separate sections, size bound) and EXP-14 (no platform write call made)
- [x] 6.6 (beta finding, design D7) Surface FinanceModel's `payload.benchmark` in `get_experiment_result` as a compact `comparison` (strategy -> key metrics table, optimizer final and average weights, window, risk-free, units) within the byte limit. Verify EXP-16 unit tests (table and weights from the mock FinanceModel result; no block -> no comparison; unreviewed values dropped; size pressure drops the full block and trims weights but keeps the table; contract-valid response, no leaks)

## 7. Plan tools (spec plan-tools)

- [x] 7.1 Implement `get_plan_version`. Verify PLN-01 and PLN-02 against the mock (lineage, status, checksum; `NOT_FOUND`)
- [x] 7.2 Implement `get_plan` and `list_plan_versions`. Verify PLN-02 (head and revision) and PLN-03 (pagination without duplicates). These use platform `GET /v1/plans/{plan_id}` and `GET /v1/plans/{plan_id}/versions` (G-1 resolved); until the platform release with those routes exists in the environment, both tools report `DEPENDENCY_UNAVAILABLE`
- [x] 7.3 Implement `create_override_version` (child only, `expected_revision`, derived key, synthetic guard). Verify PLN-04 (child created, parent unchanged, `CONFLICT` on a stale revision, `no_effect` flag), PLN-05 (`IMMUTABLE_RECORD`) and PLN-09 (non-synthetic → `OPERATION_NOT_PERMITTED`)
- [x] 7.4 Implement `validate_plan_version`. Verify PLN-06 (sum 1.07 → `invalid` with the finding passed through unchanged)
- [x] 7.5 Implement `publish_plan_version`. Verify PLN-07 (validated → `publication_id`; invalid → `PRECONDITION_FAILED`; a duplicate gives one publication) and PLN-08 (an `execute`/`mode` field → `VALIDATION_FAILED`)
- [x] 7.6 Add the tool-inventory deny-list build check (execute, trade, order, payment, wallet, execution). Verify the PLN-08 inventory negative test fails the build

## 8. Infrastructure, wiring and permissions (spec tool-environment-wiring)

- [x] 8.1 Write the CDK stack: one function per tool from one asset, arm64, no VPC, per-tool timeouts, alias `current`, and three role classes with the contract permission boundary (environment-tag and name-prefix denies, single account) plus explicit denies (execution routes, approve/cancel, SageMaker, live-financial, other environments). Verify ENVW-07 cost-check, ENV-05 policy-scan and the ENVW-02 missing-boundary negative test on the synthesized template
- [x] 8.2 Resolve every producer reference from same-environment SSM at deploy and run time. Verify ENVW-01 (synth for gamma references only `/finplan/gamma/...`; the leak scan finds no literals)
- [x] 8.3 Create invoke grants from the single direct-test principal name in `/finplan/<env>/financelambdastool/config/direct-test-principal-name` (resolved to a principal in the deploying account at deploy time; prod grant on read-only tools only), the pipeline test/smoke roles and the optional Gateway principal `/finplan/<env>/financeagent/agent/gateway-principal-ref`. Validate the name (reject `arn:` values, wildcards, account IDs, the account root and lists; absent parameter → no direct-test grant). Verify ENVW-03, ENVW-04 and ENVW-08 synth tests: no Gateway grant when its parameter is absent, ARN value rejected, no direct-test grant when its parameter is absent, prod direct-test grant on read-only tools only, no wildcards (LT-OQ-5 RESOLVED 2026-10-07)
- [x] 8.4 Add the IAM policy-simulation unit tests for cross-environment denial within the single account. Verify ENVW-02 (gamma role → prod plan API and a prod-tagged resource denied in simulation)
- [x] 8.6 Publish `/finplan/<env>/financelambdastool/config/budget-enforced-role-names` with the `submitter` role name for the FinancialPlanning budget deny action at 100% of USD 50. Verify with a synth unit test that the parameter lists the `submitter` role only and contains no ARN or account ID
- [ ] 8.5 Document direct invocation for the project owner (fixture payloads, the SSM parameter that names the direct-test principal, expected responses; no principal names or ARNs in the docs). Verify ENVW-04 by running the documented local command offline (ENVW-05) and, once beta exists, the documented beta command as the project owner. Requires the bootstrap to have run (9.6, approved in principle; runs once its IaC is implemented)

## 9. Release publication and pipeline (spec tool-release-publication)

- [x] 9.1 Publish Lambda references, role-class references, the tool catalog and the release manifest at deploy time. Verify REL-01, REL-02 and REL-03 contract tests (the catalog and manifest validate against the pinned schemas; every catalog entry's parameter name follows the SSM convention)
- [x] 9.2 Implement the pre-deploy producer compatibility check (platform required, FinanceModel optional). Verify REL-06 with fixture manifests (platform absent → stage fails; model absent → proceeds)
- [x] 9.3 Define the pipeline per contracts D6 with this repo's build checks, beta/gamma direct-invocation suites and the prod read-only smoke. Include the per-call limit bound check (6.2a) in each pre-deploy step. Verify REL-05 (ENV-09) with the pipeline-structure check and REL-04 by asserting equal digests across stage manifests in a dry-run synth. Pipeline creation happens through 9.6
- [ ] 9.4 Implement rollback by `release_id` (alias move plus republishing references and the manifest with `rolled_back_from`). Verify REL-07 as a gamma rollback drill. Requires the bootstrap (9.6)
- [x] 9.5 Write the prod smoke suite (describe_capabilities plus read tools on the synthetic prod portfolio, no writes, no runs). Verify REL-08 by asserting zero write calls in a mock run
- [x] 9.6 Write the one-time bootstrap (approved in principle 2026-10-07; runs only after this IaC is implemented, first printing the exact stacks to create and a cost estimate; never run during spec work): uses the existing authenticated CLI session (root not refused), checks STS account and region against local untracked configuration, writes `/finplan/shared/financelambdastool/config/codeconnection-ref` for an existing AVAILABLE connection, creates the scoped pipeline/deploy/role-class roles with the permission boundary, writes default `tool-limits` unless present, writes each environment's `/finplan/<env>/financelambdastool/config/direct-test-principal-name` from the project-owner principal name in local untracked configuration (name only, never an ARN; refuses root), runs a source-stage dry run and prints the scoped/MFA operator recommendation. Verify REL-09 with unit tests on a mocked STS/SSM (REL-10: stack list and cost estimate printed before any change; root caller accepted; dry-run failure leaves deploy stages disabled and prints the GitHub App instruction) and the leak scan over the bootstrap code. Code done and unit-verified locally 2026-10-08; the bootstrap itself has NOT been run.

## 10. Environment integration checks

- [ ] 10.1 Beta direct-invocation conformance: run every tool against beta platform fixtures with the pipeline test role, and confirm the project owner's direct-test principal can invoke a read tool. Check duplicate requests, budget rejection, schema-version mismatch, partial results, conflict and `DEPENDENCY_UNAVAILABLE` for experiment tools before FinanceModel. Verify the integration-beta suite passes (OWN-06, ENV-15). Requires the bootstrap to have run (9.6, approved in principle) and the platform beta release
- [ ] 10.2 After the FinanceModel beta release, run fixture stub experiments end to end (submit, then status, then result, with infeasible and failed fixtures). Verify that EXP-01 and EXP-10 pass in integration-beta and that only one run exists per duplicate pair
- [ ] 10.3 Gamma: repeat 10.1 and 10.2 plus isolation (a gamma role calling prod is denied; all resolved references are gamma). Verify the gamma suite (ENV-03, OWN-05)
- [ ] 10.4 With FinanceAgent in beta: confirm Gateway registration reads the catalog, and that a Gateway-path `get_plan_version` returns the same `plan_version_id` and checksum as a direct platform read. Verify PLN-01 and ENV-15 through Gateway. BLOCKED by LT-OQ-1 and the FinanceAgent release

## Requirement-to-test mapping

Test types: unit, contract (schema/conformance with fixtures and mocks), integration-beta, gamma, smoke. Contract-level IDs (CS-, OWN-, ENV-, ID-) refer to `establish-cross-repo-contracts`.

| Requirement (spec) | Test ID | Type |
|---|---|---|
| Validation against the pinned contract package (tool-request-handling) | TRH-01 | unit + contract |
| Contract major negotiation | TRH-02 | unit + contract + integration-beta (schema version mismatch) |
| Identifier format checks | TRH-03 | unit |
| Trusted invocation source and caller identity | TRH-04 | unit + integration-beta (Gateway path after LT-OQ-1) |
| Environment check | TRH-05 | unit + gamma |
| Idempotency on write tools | TRH-06 | unit + contract + integration-beta (duplicate requests) |
| Error envelope mapping | TRH-07 | contract (CS-05) |
| Rate limiting and dependency failures are retryable | TRH-08 | unit + contract |
| Trusted artifact references only | TRH-09 | unit + contract (leak scan over responses, CS-08) |
| Compact, size-bounded responses | TRH-10 | unit + integration-beta |
| Stateless, non-authoritative adapters | TRH-11 | unit + contract (OWN-04) |
| Structured audit logging | TRH-12 | unit |
| Capability description response (capability-discovery) | CAP-01 | contract + integration-beta + smoke |
| Dependency availability per tool | CAP-02 | unit + integration-beta |
| Declared limits and capabilities are reported, not inferred | CAP-03 | unit (granularity, per-category budget limits) |
| No side effects and no secrets | CAP-04 | unit + smoke |
| Refresh delegates to the platform ingestion operation (market-data-tools) | MKT-01 | contract + integration-beta |
| Tools never call the market-data provider | MKT-10 | unit (build check: no provider import or secret read; mock asserts single platform call) |
| Refresh is idempotent | MKT-02 | contract + integration-beta (duplicate requests) |
| Requests outside declared provider capabilities are rejected | MKT-03 | contract |
| Provider throttling and incomplete responses are passed through | MKT-08 | unit + contract (mock platform rate-limit and partial-response fixtures) |
| Budget-capped refresh | MKT-04 | unit + contract (budget rejection) |
| Query returns snapshot metadata and observation summaries | MKT-05 | contract + integration-beta |
| Query reads approved snapshots and surfaces lineage | MKT-11 | contract (approved-only, lineage fixtures) + integration-beta |
| Partial coverage is explicit | MKT-06 | contract (partial results) |
| Query is read-only and environment-scoped | MKT-07 | unit + gamma |
| No retrieved market data in the repository | MKT-09 | unit (fixture provenance build check, offline CI) |
| Asynchronous submission through the FinanceModel job interface (experiment-tools) | EXP-01 | contract + integration-beta |
| Model-backed tools gated on producer availability | EXP-02 | unit + integration-beta (OWN-06) |
| Snapshot compatibility check | EXP-03 | unit + contract |
| Configuration validation and identity | EXP-04 | unit (ID-02 helpers) + contract |
| Tool budget pre-check | EXP-05 | unit + contract + integration-beta (budget rejection) |
| Per-call limits bounded by the budget allocation | EXP-15 | unit (pre-deploy check with fixture allocations) |
| Dry run | EXP-06 | contract + integration-beta |
| Tools never approve paid jobs | EXP-07 | unit (static check) + gamma (approve denied by IAM) |
| Restricted run purposes | EXP-08 | unit |
| Job status reporting | EXP-09 | contract + integration-beta |
| Completion status separate from solution status | EXP-10 | contract (CS-07) + integration-beta |
| Results for non-terminal runs | EXP-11 | contract |
| Partial results are flagged | EXP-12 | contract (partial results) + integration-beta |
| Compact result summary | EXP-13 | unit + contract |
| Results never become plan state through tools | EXP-14 | unit (no write calls) |
| Plan tools use only the platform plan API (plan-tools) | PLN-01 | contract + integration-beta + gamma (website/agent agreement) |
| Read plan and plan version | PLN-02 | contract + integration-beta + smoke |
| List plan versions | PLN-03 | contract + integration-beta |
| Overrides create child versions | PLN-04 | contract + integration-beta (duplicate, conflict, no-effect) |
| Immutable versions are never edited | PLN-05 | contract |
| Validate plan version | PLN-06 | contract + integration-beta |
| Publish an exact validated version | PLN-07 | contract + integration-beta + gamma (ID-08) |
| Publication never executes trades | PLN-08 | unit (inventory check, unknown fields) + ENV-05 scan |
| Synthetic portfolios only in phase 1 | PLN-09 | unit + integration-beta |
| Producer references resolved from same-environment configuration (tool-environment-wiring) | ENVW-01 | unit (synth + leak scan) + gamma |
| Cross-environment calls are impossible | ENVW-02 | unit (policy simulation, permission-boundary synth check) + gamma (ENV-03) |
| Explicit invoke permissions | ENVW-03 | unit (synth) + gamma (OWN-05) |
| Single direct-test principal per environment | ENVW-08 | unit (synth + name validation: ARN, wildcard, root and list rejected; absent → no grant) + leak scan |
| Direct invocation before Gateway | ENVW-04 | integration-beta + gamma + smoke (prod denial of non-granted principals and of direct writes) |
| Credential-free local execution | ENVW-05 | unit + contract (offline run, artifact exclusion) |
| Phase 1 fixture-backed deployment | ENVW-06 | integration-beta + gamma (ENV-15) |
| Near-zero standing cost | ENVW-07 | unit (cost check) |
| Lambda reference per tool (tool-release-publication) | REL-01 | contract + integration-beta |
| Tool catalog | REL-02 | contract + integration-beta |
| Release manifest | REL-03 | contract (ENV-06) + integration-beta |
| Same artifact promoted across environments | REL-04 | smoke (ENV-10 digest equality) |
| Pipeline stages and gates | REL-05 | unit (ENV-09 pipeline check) |
| Promotion requires compatible producers | REL-06 | unit (fixture manifests) + gamma (OWN-08) |
| Rollback by release identifier | REL-07 | gamma (rollback drill) |
| One-time pipeline bootstrap | REL-09 | unit (mocked STS/SSM) + manual bootstrap run with dry-run evidence |
| Bootstrap runs under the in-principle approval | REL-10 | unit (stack list and cost estimate printed before any change) |
| Prod smoke tests are non-mutating or synthetic | REL-08 | unit (mock) + smoke |

## Workflow follow-up

- LT-OQ-3 and LT-OQ-6 resolved by the user decisions of 2026-10-07 (LT-OQ-6: provider `yfinance` behind the platform adapter, no API key; tools never call it). The bootstrap is approved in principle and runs once its IaC (9.6) is implemented. LT-OQ-5 RESOLVED 2026-10-07: one direct-test principal per environment, the project owner, referenced through SSM by name only. Remaining blockers: LT-OQ-1 (Gateway event shape) and producer releases (platform phase 1 beta, including the snapshot observation and lineage reads; FinanceModel beta; FinanceAgent Gateway).
- Gaps G-1..G-6 were resolved by the cross-repo review (contracts D4/D10, platform P4). Tool handlers pin the contracts version that includes the tool catalog and per-tool schemas (1.0.0; 0.x in beta first).
- Archive this change after phase 1 passes gamma (10.3) and the FinanceAgent Gateway check (10.4) succeeds.
