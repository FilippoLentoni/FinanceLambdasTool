# Spec Delta

## Purpose

Defines the `refresh_market_data` and `query_market_data` tools, which let agents trigger on-demand platform ingestion and read snapshot metadata and observation summaries without bypassing the platform's ingestion, validation and storage.

## ADDED Requirements

### Requirement: Refresh delegates to the platform ingestion operation
`refresh_market_data` SHALL call only the platform ingestion operation resolved from `/finplan/<env>/financialplanning/api/ingestion-endpoint`, passing dataset, instrument, granularity and date range. It MUST NOT call market-data providers or write storage itself.

#### Scenario: Fixture refresh in beta
- **WHEN** `refresh_market_data` is invoked in beta for the fixture S&P 500 tracking-ETF daily dataset (`finance/etf-daily/<instrument>`, for example SPY) with `daily` granularity and a valid `idempotency_key`
- **THEN** the response contains the platform-minted `input_snapshot_id`, source-data timestamps, retrieval timestamp, date coverage, quality flags and the snapshot manifest checksum

### Requirement: Tools never call the market-data provider
The market-data tools SHALL reach the provider only through the platform. They MUST NOT import or call `yfinance` (the platform's phase 2 daily provider behind its provider adapter interface, with the `exchange_calendars` XNYS calendar) or any other provider library, and MUST NOT read or require a provider API key or secret; the provider needs none.

#### Scenario: Phase 2 refresh through the platform's yfinance-backed adapter
- **WHEN** `refresh_market_data` is invoked in an environment where the platform's provider adapter is configured for `yfinance`
- **THEN** the tool makes exactly one call to the platform ingestion operation, makes no outbound call to any provider host, reads no provider secret, and returns the platform's result with its lineage (provider, provider library version, retrieval timestamp) unchanged

#### Scenario: Tool artifact contains no provider library
- **WHEN** the build stage inspects the deployable artifact and the tool source
- **THEN** it finds no `yfinance`, `exchange_calendars` or other market-data provider import, and fails the build if one is present

### Requirement: Refresh is idempotent
`refresh_market_data` SHALL require an `idempotency_key` and derive the downstream key per the request-handling rules. A duplicate request MUST return the original snapshot result without a second ingestion.

#### Scenario: Duplicate refresh
- **WHEN** a caller repeats `refresh_market_data` with the same key and body
- **THEN** the same `input_snapshot_id` is returned and the platform records one ingestion

### Requirement: Requests outside declared provider capabilities are rejected
The tool SHALL reject a request whose dataset, granularity or history range the platform does not declare as supported. It MUST pass the platform's capability rejection through with `VALIDATION_FAILED` and MUST NOT treat daily support as intraday support.

#### Scenario: Intraday request against daily-only provider
- **WHEN** `refresh_market_data` requests `intraday` granularity and the provider declares only `daily`
- **THEN** the tool returns `VALIDATION_FAILED` naming the unsupported granularity, and no ingestion runs

### Requirement: Provider throttling and incomplete responses are passed through
The platform ingestion may be rate-limited by the upstream provider and may back off and retry internally. `refresh_market_data` SHALL pass a platform `RATE_LIMITED` or `DEPENDENCY_UNAVAILABLE` outcome through with its `retryable` flag and MUST NOT add its own provider retries. When the platform records quality flags for an empty or partial provider response, the tool MUST return those flags exactly as reported and MUST NOT present the refresh as complete.

#### Scenario: Upstream provider throttled
- **WHEN** the platform ingestion returns `RATE_LIMITED` with `retryable` true after its own backoff
- **THEN** the tool returns `RATE_LIMITED` with `retryable` true, makes no further ingestion call in the same invocation, and creates no snapshot

#### Scenario: Partial provider response
- **WHEN** the platform ingestion commits a snapshot whose quality flags record an empty or partial provider response (for example `missing_sessions`)
- **THEN** the tool returns the snapshot with those quality flags unchanged and the actual coverage, and does not claim the requested range is fully covered

### Requirement: Budget-capped refresh
When the platform reports the project budget enforcement as active, `refresh_market_data` SHALL return `BUDGET_EXCEEDED` with `retryable` false and MUST NOT retry the call.

#### Scenario: Cap reached
- **WHEN** the platform ingestion returns `BUDGET_EXCEEDED`
- **THEN** the tool returns `BUDGET_EXCEEDED` with `retryable` false, and no snapshot is created

### Requirement: Query returns snapshot metadata and observation summaries
`query_market_data` SHALL take an `input_snapshot_id` with optional instrument and date filters. It returns snapshot metadata, the observation kinds present (`completed_daily`, `intraday_partial`), per-instrument summary statistics and trusted references to the full data. It MUST read only through platform APIs.

#### Scenario: Query a fixture snapshot
- **WHEN** `query_market_data` is invoked with the `input_snapshot_id` of a fixture S&P 500 tracking-ETF daily snapshot (for example SPY)
- **THEN** the response contains the manifest checksum, coverage start/end, quality flags, observation kind `completed_daily` only, the ETF instrument's observation count and a trusted artifact reference to the curated data

#### Scenario: ETF series kept distinct from index level and universe
- **WHEN** `query_market_data` is invoked for an ETF daily snapshot
- **THEN** the response reports the dataset `finance/etf-daily/<instrument>` exactly as the platform declares it, and never relabels it as the index level or the constituent universe

### Requirement: Query reads approved snapshots and surfaces lineage
`query_market_data` SHALL return observations only from snapshots whose platform `status` is `approved`. It MUST surface the lineage the platform recorded (provider, provider library version, retrieval timestamp) and the platform's quality flags unchanged.

#### Scenario: Lineage surfaced for a provider-backed snapshot
- **WHEN** `query_market_data` reads an approved snapshot ingested through the platform's `yfinance` adapter
- **THEN** the response includes the provider name, the pinned provider library version and the retrieval timestamp exactly as the platform recorded them, together with the snapshot's quality flags

#### Scenario: Snapshot not approved
- **WHEN** `query_market_data` is invoked for a snapshot whose platform `status` is `committed` or `expired`
- **THEN** the tool returns `PRECONDITION_FAILED` with the snapshot `status` in `details`, and returns no observation summary

### Requirement: Partial coverage is explicit
When the stored coverage does not span the requested range, `query_market_data` SHALL return the actual coverage, `partial` true and the platform's quality flags such as `missing_sessions`. It MUST NOT fill, interpolate or fabricate observations.

#### Scenario: Requested range exceeds coverage
- **WHEN** a query asks for a range that starts before the snapshot's coverage
- **THEN** the response has `partial` true, the real coverage start, and no observations dated before it

### Requirement: Query is read-only and environment-scoped
`query_market_data` SHALL be read-only and resolve snapshots only in its own environment. A snapshot ID that does not exist in that environment MUST return `NOT_FOUND`.

#### Scenario: Prod snapshot ID queried from gamma
- **WHEN** a gamma `query_market_data` receives a snapshot ID that exists only in prod
- **THEN** it returns `NOT_FOUND` and no prod resource is called

### Requirement: No retrieved market data in the repository
The repository SHALL NOT contain market data retrieved from any real provider, because the repository is public and the phase 2 provider's terms are personal/research use. Market-data fixtures and test payloads MUST be synthetic and produced by the mock provider or contract-package fixtures, and CI MUST NOT call a live provider.

#### Scenario: Fixture provenance check
- **WHEN** the build stage scans the market-data fixtures under `tests/`
- **THEN** every fixture carries `synthetic: true` and a mock-provider lineage, and the build fails on any fixture whose lineage names a real provider such as `yfinance`

#### Scenario: Offline CI run
- **WHEN** the market-data tool tests run in the build stage
- **THEN** they use only the in-process mock platform and mock provider fixtures and make no network call to a market-data provider
