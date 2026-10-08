# Tools

The full inventory and role classes are in the [README](../README.md#tools) and in
`src/finplan_tools/core/registry.py` (`CATALOG`). This page documents tools added after
`add-mcp-tool-adapters`.

## `production_strategy` (contracts 1.1.0)

Reads or changes the production strategy that the environment's daily recommendation job runs.
FinanceModel owns the setting (`/finplan/<env>/financemodel/config/production-strategy`) and its
selection operation is the only writer. The tool calls only that operation:

| action  | FinanceModel call               | needs                                                                 |
|---------|---------------------------------|-----------------------------------------------------------------------|
| `get`   | `GET /v1/production-strategy`   | any authenticated caller                                              |
| `set`   | `PUT /v1/production-strategy`   | `strategy_id`, `idempotency_key`, `confirmed_by_user: true`, group `plan_publisher` |
| `clear` | `PUT /v1/production-strategy`   | `idempotency_key`, `confirmed_by_user: true`, group `plan_publisher`  |

- **Kind:** state-changing, role class `plan-writer`. In prod it is reachable through the Gateway only,
  like every state-changing tool.
- **Checks before any FinanceModel call:**
  - schema validation (`VALIDATION_FAILED`);
  - `idempotency_key` for `set` and `clear` (`VALIDATION_FAILED`);
  - environment (`FORBIDDEN`);
  - group `plan_publisher` (`FORBIDDEN`, reason `group_required`);
  - `confirmed_by_user: true` (`PRECONDITION_FAILED`, reason `confirmation_required`);
  - FinanceModel release with contract 1.1.0 or later (`DEPENDENCY_UNAVAILABLE`, reason
    `operation_not_released`).
- **Groups:** the direct-test principal and the pipeline test role act for the project owner
  (`plan_publisher`). Gateway callers have no verified groups until the Gateway-to-Lambda caller
  propagation is decided (FinanceAgent FA-OQ-2, LT-OQ-1), so `set` and `clear` through the Gateway
  fail closed with `FORBIDDEN` for now. `get` works through every source.
- **Confirmation:** `confirmed_by_user` is a tool-only field. The 1.1.0 request schema does not
  declare it, so the tool removes it before schema validation, checks it, and forwards
  `confirmed_by_user: true` to FinanceModel, which enforces it again. The agent sets it only after
  the user's explicit confirmation turn.
- **Idempotency:** the tool forwards the derived key (`lt_` + SHA-256 of
  `identity|env|production_strategy|idempotency_key`). A repeat with the same key returns the
  original result.
- **Errors:** FinanceModel decides validity. Its errors pass through unchanged, for example
  `VALIDATION_FAILED` with `details.reason` `no_evaluation_evidence` for a strategy without
  evaluation evidence.
- **No side doors:** the role has an explicit deny on SSM writes to
  `/finplan/<env>/financemodel/config/*`. The tool never submits jobs, publishes plans or records
  executions.

### Example requests

Set (after the user confirmed):

```json
{
  "action": "set",
  "strategy_id": "buy_and_hold",
  "idempotency_key": "select-bah-0001",
  "confirmed_by_user": true,
  "synthetic": true
}
```

Get:

```json
{
  "action": "get"
}
```

### Example responses

Nothing selected (daily recommendations are off):

```json
{
  "environment": "beta",
  "action": "get",
  "strategy": null,
  "changed": false
}
```

After `set`:

```json
{
  "environment": "beta",
  "action": "set",
  "strategy": {
    "strategy_id": "buy_and_hold",
    "environment": "beta",
    "selected_at": "2026-01-09T15:00:00Z",
    "selected_by": "synthetic-operator",
    "contract_version": "1.1.0",
    "synthetic": true
  },
  "changed": true,
  "synthetic": true
}
```
