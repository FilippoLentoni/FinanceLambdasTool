# Selected strategy MCP tool

`recommend_portfolio` is a read-only recommendation tool pinned to contracts 1.5.0.

Its input schema is `finance/v1/tools/recommend-portfolio-invocation-request`; the original 1.2
explicit-only request schema remains unchanged.

Call it with `{}` to load the saved beta paper portfolio and latest approved completed market
session automatically. FinanceModel marks the persisted instrument quantities and cash, builds the
trained state and returns policy targets plus proposed fractional share and value changes. The full
response includes all instrument targets, cash, saved book identity/revision, price dates and policy/
data provenance. The tool does not initialize a book or update positions from recommendations.

The existing explicit mode remains supported: supply `input_snapshot_id`, paired `as_of`, and
complete `holdings` (`weights`, `cash_weight`, `portfolio_value`, `high_watermark`). Alternatively
`portfolio_id` selects a saved book; it cannot be mixed with explicit holdings. Requests with no
explicit snapshot use the approved default dataset. The tool gates inference on FinanceModel 1.3.0
so an older producer cannot silently receive unsupported default requests.

The result remains advisory/paper, with indicative fractional quantities at the reference close.
Rounding, transaction costs and execution price changes can affect executable quantities. There is
no training or strategy selection change; recommendations do not prove executed trades. Saved-book
responses include a durable `decision_id`; only a separately confirmed paper resolution updates holdings.

Both MCP Gateways expose `get_portfolio_decision`, `list_portfolio_decisions`,
`resolve_portfolio_decision`, `get_portfolio_history`, `list_market_snapshots`,
`explain_portfolio_decision`, `compare_portfolio_decisions`, `evaluate_portfolio_decision`,
`record_agent_activity` and `list_agent_activity`. Default history/snapshot reads return the latest
three stored records. Generic explanation and performance tools take issued `decision_id` values
from either model family; use them for accepted-book history rather than the legacy classical analysis path.

Accept/reject requires the exact stored decision, source `expected_revision`, explicit
`confirmed_by_user=true` and an `idempotency_key`. The resolution target on either Gateway must
receive `X-Finplan-User-Token` with the human's public-client access token. The adapter verifies its
signature/issuer/client/lifetime and signed viewer/researcher/plan_editor/plan_publisher membership;
machine/direct-test markers cannot approve. Acceptance records simulated fractional fills at the
issued reference prices, disclosed costs and one new revision. Rejection leaves holdings unchanged.
Retries preserve the downstream idempotency key; the Platform also prevents permanent double application.

All deployed adapters save sanitized successful/failed invocation receipts through the Platform
activity API, excluding credentials and private grants. Explicit archive calls do not recursively
archive themselves. A failed durable archive is reported rather than presenting an unarchived success.

The adapter resolves `/finplan/<env>/financemodel/api/strategy-function-ref` and verifies that it
names the expected function in the same environment, region and account. It invokes the strategy
Lambda directly with caller and correlation metadata. It never follows caller-supplied endpoints
or retries an invocation automatically. Producer errors propagate; malformed/oversized responses
fail closed. The existing Gateway limitation on end-user identity at Lambda targets still applies:
Gateway policy evaluates the authenticated user's token; the adapter's producer audit identifies
the Gateway invocation, not a separately verified user subject.

The strategy service's deadline is 270 seconds, the adapter's SDK read deadline is 280 seconds,
the MCP Lambda target is 300 seconds and the beta agent's MCP request deadline is 330 seconds.
Legacy tool-limit documents inherit the new deadline while retaining existing overrides. Only
the reader role can invoke the exact same-environment strategy function, including Lambda's
authorization of the unqualified request against its `$LATEST` resource. Published versions,
aliases, other functions and training/configuration writes remain denied.

FinancialPlanning → FinanceModel → FinanceLambdasTool → FinanceAgent is the initial beta release
order. Supported artifact/parameter updates subsequently use strategy activation and do not
change this tool's interface. The complete lifecycle is in FinanceAgent `docs/strategy-lifecycle.md`.
