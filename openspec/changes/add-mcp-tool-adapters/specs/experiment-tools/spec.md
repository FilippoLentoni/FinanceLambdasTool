# Spec Delta

## Purpose

Defines the `submit_experiment`, `get_job_status` and `get_experiment_result` tools, which submit and track asynchronous FinanceModel experiment runs. Before submitting, they validate snapshot and configuration compatibility and budgets on the server side. They report execution completion separately from solution quality.

## ADDED Requirements

### Requirement: Asynchronous submission through the FinanceModel job interface
`submit_experiment` SHALL call only FinanceModel `submit_job` at `/finplan/<env>/financemodel/api/job-endpoint`. It MUST return the minted `run_id`, `configuration_id` and initial state immediately, without waiting for the job. It MUST NOT start compute directly or mint `run_id`.

#### Scenario: Valid fixture submission
- **WHEN** a valid fixture backtest is submitted in beta and FinanceModel is released there
- **THEN** the tool returns the FinanceModel-minted `run_id`, the `configuration_id` and state `queued` or `awaiting_approval` within the Lambda timeout

### Requirement: Model-backed tools gated on producer availability
When no compatible FinanceModel release is recorded in the Lambda's environment, the experiment tools SHALL return `DEPENDENCY_UNAVAILABLE`, `retryable` false, without contacting any other environment.

#### Scenario: Tools released before model service
- **WHEN** `submit_experiment` is invoked in beta before any FinanceModel beta release exists
- **THEN** it returns `DEPENDENCY_UNAVAILABLE` and no job is submitted anywhere

### Requirement: Snapshot compatibility check
Before submitting, `submit_experiment` SHALL confirm through the platform API that the `input_snapshot_id` exists in the same environment and has the request's `domain`. The snapshot coverage MUST span the experiment date range. Otherwise it returns `PRECONDITION_FAILED` naming the mismatch.

#### Scenario: Range outside snapshot coverage
- **WHEN** the experiment's evaluation window ends after the snapshot's coverage end
- **THEN** the tool returns `PRECONDITION_FAILED` with the snapshot coverage in `details`, and no job is submitted

#### Scenario: Unknown snapshot
- **WHEN** the `input_snapshot_id` does not exist in the environment
- **THEN** the tool returns `NOT_FOUND` and no job is submitted

### Requirement: Configuration validation and identity
`submit_experiment` SHALL validate the configuration against the domain adapter schema of the declared `domain_schema_version`. It computes the `configuration_id` per the contract canonicalization. If FinanceModel returns a different `configuration_id`, the tool MUST report `INTERNAL` and log both values.

#### Scenario: Invalid strategy parameter
- **WHEN** the configuration sets a risk-aversion value outside the schema's allowed range
- **THEN** the tool returns `VALIDATION_FAILED` pointing at that field, and no job is submitted

#### Scenario: Unsupported experiment type
- **WHEN** the requested job type is not listed as supported by FinanceModel in that environment
- **THEN** the tool returns `DEPENDENCY_UNAVAILABLE` or `VALIDATION_FAILED`, as reported by FinanceModel, and no run is recorded

### Requirement: Tool budget pre-check
Before a non-dry-run submission, `submit_experiment` SHALL obtain FinanceModel's dry-run estimate and `budget_category`. It MUST return `BUDGET_EXCEEDED` with the estimate, category and limit in `details`, and submit nothing, when the estimate exceeds that category's per-call limit in `tool-limits` or the category has no limit. Category and project budgets stay with FinanceModel and the USD 50 Budgets deny.

#### Scenario: Budget rejection
- **WHEN** a `cpu_research` dry-run estimate is USD 1.40 and the configured `cpu_research` per-call limit is the default USD 1.00
- **THEN** the tool returns `BUDGET_EXCEEDED`, `retryable` false, with the estimate, category and limit in `details`, and FinanceModel records no run

#### Scenario: Category without an experiment allowance
- **WHEN** a dry-run estimate is attributed to a category whose per-call limit is 0 or absent, such as `bedrock_explanations`, and the estimate is above 0
- **THEN** the tool returns `BUDGET_EXCEEDED` and no job is submitted

#### Scenario: GPU run within the per-call limit
- **WHEN** a `gpu` dry-run estimate is USD 3.00 and the `gpu` per-call limit is the default USD 5.00
- **THEN** the tool submits the job, and returns state `awaiting_approval` as reported by FinanceModel, because every GPU run needs explicit user approval with the cost estimate

#### Scenario: Phase 1 fixture job
- **WHEN** a phase 1 fixture job type is submitted with an estimate of 0
- **THEN** the per-call check passes for any configured limit

#### Scenario: FinanceModel budget rejection passed through
- **WHEN** the estimate is within the tool limit but FinanceModel rejects the submission with `BUDGET_EXCEEDED`
- **THEN** the tool returns `BUDGET_EXCEEDED` unchanged and records no retry

### Requirement: Per-call limits bounded by the budget allocation
Per-call tool limits SHALL be derived from the USD 50 category allocation at `/finplan/shared/financialplanning/config/budget-allocation`: each MUST be at most `per_call_max_fraction` (default 0.2) of its category. Defaults are `cpu_research` USD 1.00, `gpu` USD 5.00 and 0 elsewhere. The pre-deploy check MUST fail on a limit above its bound or a category absent from the allocation; run time uses only the static limits.

#### Scenario: Default limits within the default allocation
- **WHEN** the pre-deploy check runs with the default allocation (`platform_infra` 8, `cpu_research` 7, `bedrock_explanations` 5, `gpu` 25, `reserve` 5) and the default `tool-limits`
- **THEN** the check passes, because USD 1.00 ≤ 0.2 × 7 and USD 5.00 ≤ 0.2 × 25

#### Scenario: User lowers an allocation below the limit bound
- **WHEN** the user edits the allocation so that `gpu` is USD 10 while the `gpu` per-call limit stays USD 5.00
- **THEN** the pre-deploy check fails with a message naming the category, the limit and the bound USD 2.00, and the deployment does not proceed

### Requirement: Dry run
`submit_experiment` SHALL accept `dry_run` true. It performs every validation, returns the `configuration_id` and cost estimate, and MUST NOT create a run.

#### Scenario: Dry run returns estimate only
- **WHEN** a caller submits a valid request with `dry_run` true
- **THEN** the response contains the `configuration_id` and estimate, and no `run_id`

### Requirement: Tools never approve paid jobs
The experiment tools SHALL NOT expose or call any approval operation. A run waiting for human approval MUST be reported as state `awaiting_approval`, with no option to approve through any tool.

#### Scenario: Run needs approval
- **WHEN** FinanceModel returns state `awaiting_approval` for a submission
- **THEN** the tool returns that state and a message that a human approver must act, and no tool can change it

### Requirement: Restricted run purposes
`submit_experiment` SHALL allow the purposes `research`, `tuning` and `holdout_evaluation` only. A `production_candidate` purpose MUST be rejected with `FORBIDDEN`.

#### Scenario: Production candidate requested
- **WHEN** a caller submits with purpose `production_candidate`
- **THEN** the tool returns `FORBIDDEN` and makes no FinanceModel call

### Requirement: Job status reporting
`get_job_status` SHALL return the FinanceModel run state, transition timestamps, elapsed runtime and, for terminal runs, `completion_status`. It MUST NOT return storage locations.

#### Scenario: Running job
- **WHEN** `get_job_status` is called for a running run
- **THEN** the response has state `running`, the transition timestamps and no `completion_status`

### Requirement: Completion status separate from solution status
`get_experiment_result` SHALL report `completion_status` and `solution_status` as separate fields. An infeasible, unbounded or no-effect outcome MUST be returned as a successful tool call with `completion_status` `succeeded`, never as a tool error.

#### Scenario: Infeasible optimization
- **WHEN** the run finished and proved the constraints infeasible
- **THEN** the result has `completion_status` `succeeded` and `solution_status` `infeasible`, and the tool response is not an error

#### Scenario: Failed run
- **WHEN** the run's container crashed
- **THEN** the result has `completion_status` `failed`, no `solution_status`, and the FinanceModel error envelope in `error`

#### Scenario: Optimal result
- **WHEN** an optimizer run found an optimal solution
- **THEN** the result has `completion_status` `succeeded` and `solution_status` `optimal`

### Requirement: Results for non-terminal runs
`get_experiment_result` for a run that is not terminal SHALL return `PRECONDITION_FAILED`, with the current state in `details`.

#### Scenario: Result requested too early
- **WHEN** `get_experiment_result` is called while the run is `queued`
- **THEN** it returns `PRECONDITION_FAILED` with `details.state` `queued`

### Requirement: Partial results are flagged
When a run ended `timed_out` or `cancelled`, or FinanceModel marks outputs incomplete, `get_experiment_result` SHALL return `artifacts_complete` false. Only the artifact references FinanceModel lists are returned, and the result MUST NOT be presented as a usable plan input.

#### Scenario: Timed-out backtest
- **WHEN** a backtest ended `timed_out` with some partial outputs
- **THEN** the result has `completion_status` `timed_out`, `artifacts_complete` false, only the listed references, and no summary metrics computed from missing periods

### Requirement: Compact result summary
`get_experiment_result` SHALL return a bounded summary: headline metrics, evaluator version, dataset checksum, `model_version`, `configuration_id`, `input_snapshot_id` and trusted artifact references with checksums. Portfolio performance, model accuracy and compute cost MUST appear in separate sections.

#### Scenario: Succeeded backtest summary
- **WHEN** a succeeded backtest result is requested
- **THEN** the response includes the metric sections separately, the lineage identifiers and artifact references, stays under the byte limit and contains no bucket names or keys

### Requirement: Results never become plan state through tools
Experiment tools SHALL NOT create plan versions from results. Promotion of model output into a plan version MUST remain the platform's staged-output acceptance path.

#### Scenario: Result with candidate allocations
- **WHEN** a result contains candidate allocations
- **THEN** the tool returns them only as summary and references, and no platform write is made
