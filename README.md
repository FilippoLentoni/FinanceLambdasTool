# FinanceLambdasTool

Thin, stateless MCP adapter Lambdas for the finplan project (OpenSpec change
`add-mcp-tool-adapters`). Each tool is one AWS Lambda function that validates a request against the
pinned `finplan-contracts` schemas and calls exactly one producer in the same environment:

- the FinancialPlanning plan and ingestion APIs (plans, versions, publications, snapshots, market data);
- the FinanceModel job API (experiments: submit with dry run, status, result).

## Ownership boundary

- **No authoritative storage.** The tools own no table, bucket or cache of producer data. Only
  references (SSM parameters) are cached in memory, for at most the reference TTL.
- **Mints no identifiers.** `plan_version_id`, `publication_id`, `input_snapshot_id` and `run_id`
  come from the producers. Requests that try to supply them are rejected.
- **No trading, payment or wallet functions.** No tool executes, trades, orders, pays, approves or
  cancels anything. A build check enforces this tool inventory deny-list.
- **Trusted references only.** Storage URIs, ARNs and paths are rejected as inputs and never
  returned. Artifacts appear only as contract artifact references.

## Tools

| Tool | Kind | Producer |
|---|---|---|
| `describe_capabilities` | read | configuration and release manifests |
| `query_market_data` | read | platform snapshots and observations |
| `get_plan`, `get_plan_version`, `list_plan_versions` | read | platform plan API |
| `get_job_status`, `get_experiment_result` | read | FinanceModel job API |
| `refresh_market_data` | write | platform ingestion |
| `submit_experiment` | write | FinanceModel job API (dry run, then submit) |
| `create_override_version`, `validate_plan_version`, `publish_plan_version` | write | platform plan API |

Out of scope: trade execution or execution recording, live trading, Coinbase, AgentCore payments,
wallet spending, approving paid jobs, and automated rewriting of risk preferences.

## Layout

- `src/finplan_tools/core`: the shared request pipeline (identity, contract major, validation,
  environment, idempotency, errors, bounds, references, audit) and the tool registry.
- `src/finplan_tools/backends`: typed producer clients and the SigV4 transport.
- `src/finplan_tools/tools`: one module per tool.
- `src/finplan_tools/handler.py`: the Lambda entry point (`finplan_tools.handler.handler`).
- `testing/finplan_tools_testing`: test-only mock producers and synthetic scenarios. They are never
  packaged into a deployable artifact.
- `infra/`: the CDK app. `config/`: non-secret defaults. `scripts/`: build gates and the contract pin.
- `tests/{unit,contract}` run offline. `tests/{integration,smoke}` run only in deployed stages.

## Running tests offline

No AWS credentials or network access are needed:

```bash
uv sync
uv run pytest tests/unit tests/contract
uv run python scripts/build_gates.py --stage pre
```

The offline suites force fake credentials and disable the instance metadata service, so an
accidental real AWS call fails.

## Contract pin

`finplan-contracts` is pinned by exact version and SHA-256 digest in `contracts-pin.json`,
`pyproject.toml` and `uv.lock`. Run `uv run python scripts/check_contracts_pin.py` to verify the pin.
To adopt a new contract release, run
`uv run python scripts/check_contracts_pin.py --repin [--from <wheel-or-dir>]` and then `uv sync`.

## Direct invocation

See `docs/direct-invocation.md` for the event shape the project owner uses before the Gateway
exists.
