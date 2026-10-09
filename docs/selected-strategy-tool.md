# Selected strategy MCP tool

`recommend_portfolio` is a read-only tool with the finance-domain 1.2.0 recommendation contracts.
The caller supplies an approved snapshot, completed session and observed portfolio state. The
tool returns the selected strategy's full allocations and provenance without training, execution,
or selection changes. An unpromoted policy remains beta advisory/paper.

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
the reader role can invoke the exact same-environment strategy function; other functions and
training/configuration writes remain denied.

FinancialPlanning → FinanceModel → FinanceLambdasTool → FinanceAgent is the initial beta release
order. Supported artifact/parameter updates subsequently use strategy activation and do not
change this tool's interface. The complete lifecycle is in FinanceAgent `docs/strategy-lifecycle.md`.
