# Spec Delta

## Purpose

Defines the common request-handling behavior shared by every FinanceLambdasTool MCP adapter Lambda: contract and schema validation, identity, invocation-source and environment checks, idempotency, error mapping, bounded responses and trusted-reference-only artifact handling.

## ADDED Requirements

### Requirement: Validation against the pinned contract package
Every tool request and response SHALL validate against the tool's input and output schemas in the exact `finplan-contracts` version this release pins. A request that fails validation MUST be rejected before any downstream call, with `VALIDATION_FAILED`, `retryable` false and `details` naming the failing JSON pointer.

#### Scenario: Missing required parameter
- **WHEN** `get_plan_version` is invoked without `plan_version_id`
- **THEN** the tool returns `VALIDATION_FAILED` with a `details` pointer to `/plan_version_id` and makes no platform call

#### Scenario: Response conformance
- **WHEN** any tool returns a success response
- **THEN** the response validates against the tool's output schema in the pinned contract version

### Requirement: Contract major negotiation
A request SHALL declare the contract major it was built against. A request with a major the release does not serve MUST fail with `UNSUPPORTED_CONTRACT_VERSION`, listing the served majors. A request built under an earlier minor of a served major MUST be accepted.

#### Scenario: Schema version mismatch
- **WHEN** a caller sends a request declaring contract major 2 to a release serving only major 1
- **THEN** the tool returns `UNSUPPORTED_CONTRACT_VERSION` with `details.served_majors` `[1]` and makes no downstream call

#### Scenario: Older minor accepted
- **WHEN** a request built under contract 1.0.0 reaches a release pinned to 1.2.0
- **THEN** the request is accepted and processed normally

### Requirement: Identifier format checks
Every identifier parameter SHALL be checked against the contract prefix and format before use. A malformed or wrongly prefixed identifier MUST fail with `INVALID_IDENTIFIER` naming the field. Tools MUST NOT mint platform or model identifiers.

#### Scenario: Wrong prefix
- **WHEN** `get_job_status` receives `run_id` `pv_01JA2B3C4D5E6F7G8H9JKMNPQR`
- **THEN** it returns `INVALID_IDENTIFIER` naming `run_id` and makes no FinanceModel call

#### Scenario: Caller-supplied minted identifier
- **WHEN** `create_override_version` includes a `plan_version_id` for the new version
- **THEN** it is rejected with `VALIDATION_FAILED`

### Requirement: Trusted invocation source and caller identity
Each tool SHALL accept invocations only from principals granted invoke permission in that environment. It MUST derive the caller identity from the invocation source (the Gateway-supplied identity or the direct-test principal class of the environment's single configured direct-test principal), never from a request-body field alone.

#### Scenario: Asserted identity in body ignored
- **WHEN** a direct-test invocation includes `caller: "admin"` in the request body
- **THEN** the tool records the caller as the configured direct-test principal class, and the body value is only logged as an untrusted annotation

#### Scenario: Unknown invocation source
- **WHEN** an invocation carries neither recognised Gateway context nor a configured direct-test marker
- **THEN** the tool returns `UNAUTHORIZED` and makes no downstream call

### Requirement: Environment check
Each tool SHALL know its own environment from deployment configuration. A request that declares an `environment` different from the Lambda's own MUST fail with `FORBIDDEN` before any downstream call.

#### Scenario: Gamma request to beta Lambda
- **WHEN** a request with `environment` `gamma` reaches a beta tool Lambda
- **THEN** the tool returns `FORBIDDEN` and calls no platform or model API

### Requirement: Idempotency on write tools
Every tool that changes state downstream SHALL require an `idempotency_key` in the contract format. It MUST pass a downstream key derived deterministically from the caller identity, environment, tool name and that key, so the same caller retry maps to the same downstream key and different callers never collide.

#### Scenario: Duplicate request
- **WHEN** the same caller repeats `submit_experiment` with the same `idempotency_key` and identical body
- **THEN** the tool returns the original `run_id` and FinanceModel records only one run

#### Scenario: Key reused with different body
- **WHEN** the same caller sends the same `idempotency_key` with a different body
- **THEN** the tool returns `IDEMPOTENCY_KEY_REUSED` with `retryable` false

#### Scenario: Two callers use the same key
- **WHEN** two different callers each send `idempotency_key` `k1` to `create_override_version`
- **THEN** the derived downstream keys differ and each request is processed independently

#### Scenario: Missing key
- **WHEN** a write tool is invoked without `idempotency_key`
- **THEN** it returns `VALIDATION_FAILED`

### Requirement: Error envelope mapping
Every tool error SHALL be returned as the contract error envelope with a registered code, `retryable`, `correlation_id` and `contract_version`. Downstream errors MUST keep their code and `retryable` flag. Unexpected faults MUST map to `INTERNAL` without stack traces, secrets or storage locations.

#### Scenario: Downstream conflict passed through
- **WHEN** the platform returns `CONFLICT` for a stale `expected_revision`
- **THEN** the tool returns `CONFLICT` with `retryable` false and a `correlation_id` that appears in the tool's logs

#### Scenario: Unhandled exception
- **WHEN** a tool handler raises an unexpected exception
- **THEN** the response is `INTERNAL` with no stack trace, and the full trace is only in logs under the same `correlation_id`

### Requirement: Rate limiting and dependency failures are retryable
A tool SHALL return `RATE_LIMITED` (`retryable` true) when it or a dependency throttles, and `DEPENDENCY_UNAVAILABLE` when a dependency is unreachable or not deployed. Neither outcome MUST leave a partial state change attributable to the tool.

#### Scenario: Platform throttles
- **WHEN** the platform API responds with throttling to `publish_plan_version`
- **THEN** the tool returns `RATE_LIMITED` with `retryable` true, and a retry with the same `idempotency_key` produces exactly one publication

### Requirement: Trusted artifact references only
Tools SHALL return stored artifacts only as contract trusted artifact references or platform-issued time-limited download grants. Requests MUST NOT accept storage URIs, bucket names, object keys or file paths as inputs, and responses MUST NOT contain them.

#### Scenario: Caller-chosen S3 path
- **WHEN** `submit_experiment` includes an `s3://` URI as an input or output location
- **THEN** it is rejected with `VALIDATION_FAILED` and no FinanceModel call is made

#### Scenario: Response leak scan
- **WHEN** the conformance suite runs every tool against fixtures
- **THEN** no response contains a string matching a bucket, object-key, ARN or account-ID pattern

### Requirement: Compact, size-bounded responses
Every tool response SHALL stay under a configured per-environment byte limit. Large content MUST be summarized, with the full content reachable only through trusted references. When a list is cut to fit, the response MUST set `truncated` true and return a continuation token.

#### Scenario: Large version list
- **WHEN** `list_plan_versions` would return more versions than fit the limit
- **THEN** the response has `truncated` true, a `next_token`, and is within the byte limit

### Requirement: Stateless, non-authoritative adapters
Tools SHALL hold no authoritative state. Any cache MUST be in-memory, keyed only by immutable identifiers, and never consulted for mutable heads, job status or publication state.

#### Scenario: Head read bypasses cache
- **WHEN** `get_plan` is called twice and the plan head moved between calls
- **THEN** the second response shows the new head and `revision` from the platform

### Requirement: Structured audit logging
Each invocation SHALL emit one structured log record with `correlation_id`, tool name, caller identity, environment, contract version, release ID, outcome code and the downstream identifiers involved. Log records MUST NOT include secrets, download grants or full request payloads.

#### Scenario: Audit record for publish
- **WHEN** `publish_plan_version` succeeds
- **THEN** one log record contains the `correlation_id`, caller identity, `plan_version_id` and `publication_id`, and no download grant
