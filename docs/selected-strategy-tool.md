# Selected strategy MCP tool

`recommend_portfolio` is a read-only tool pinned to contracts 1.3.0.

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
no training, execution or strategy selection change; recommendations do not prove executed trades.

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
