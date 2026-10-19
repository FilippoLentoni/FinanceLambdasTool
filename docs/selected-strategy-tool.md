# Selected strategy MCP tool

`generate_portfolio_recommendation_ppo` is a read-only recommendation tool running FinanceModel's
frozen PPO policy (contracts 1.14.0; it replaced `recommend_portfolio`, with no alias). Its schemas are
`finance/v1/tools/generate-portfolio-recommendation-ppo-request` / `-response`.

`instruments` (1-100 unique symbols) is required: FinanceModel serves the frozen PPO policy trained on
exactly that universe (for example the 8-stock AAPL, AMZN, GOOGL, META, MSFT, NVDA, TSLA, VOO policy)
or returns a typed no-policy error; it never answers with another universe's weights. The adapter
forwards `instruments` inside `request` and, unchanged, at the top level of the strategy Lambda
payload, and refuses (as `INTERNAL`) a response with a target weight outside the requested universe.

Call it with only `instruments` to load the saved beta paper portfolio and latest approved completed market
session automatically. FinanceModel marks the persisted instrument quantities and cash, builds the
trained state and returns policy targets plus proposed fractional share and value changes. The full
response includes all instrument targets, cash, saved book identity/revision, price dates and policy/
data provenance. The tool does not initialize a book or update positions from recommendations.

The existing explicit mode remains supported: supply `input_snapshot_id`, paired `as_of`, and
complete `holdings` (`weights`, `cash_weight`, `portfolio_value`, `high_watermark`). Alternatively
`portfolio_id` selects a saved book; it cannot be mixed with explicit holdings. Requests with no
explicit snapshot use the approved default dataset. The tool gates inference on FinanceModel 1.14.0
so an older producer cannot silently ignore the requested universe.

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

Public MCP responses project audit caller metadata consistently, including nested resolutions in
decision reads, history and explanations/performance analyses. Private IAM principals are represented
by stable `principal_hash` values; raw account and transport fields are omitted. The verified caller's
subject hash, role/channel and correlation joins remain available. This projection preserves all
financial fields and retries exactly, and leaves immutable producer artifacts and their checksums
unchanged. A returned checksum identifies that original artifact rather than the projected JSON bytes.

All deployed adapters save sanitized successful/failed invocation receipts through the Platform
activity API, excluding credentials and private grants. Explicit archive calls do not recursively
archive themselves. A failed durable archive is reported rather than presenting an unarchived success.
Archive writes (receipts and `record_agent_activity`) are idempotent and are retried up to three
times with exponential backoff and jitter on transport errors, throttling, 502/503/504 and
API Gateway-generated 5xx envelopes (a throttled or failed Platform Lambda, recognised by a
gateway request ID instead of the caller's correlation ID). Every tool's reads and idempotent
writes retry gateway-generated 5xx the same way; one that persists is a retryable
`DEPENDENCY_UNAVAILABLE`, never a bare `INTERNAL`. Producer-declared throttling still passes
through once. Every tool error is logged with its code, message and non-sensitive details;
`INTERNAL` errors also log the raising location.
Tool requests may be up to `request_max_bytes` (tool-limits, default 4 MiB, at most 6,000,000:
the AgentCore Gateway tool-call and Lambda synchronous payload limits are 6 MB). The
`record_agent_activity` `payload` is archived evidence: it is sanitized (credentials, private
locations, signed URLs) but not refused for path-like text such as JSON pointers or backslashes;
its other fields keep the full storage-location check.
Archived validation pointers use a typed `json_pointer` object with `encoding=base64url_utf8_segments`.
Decode each segment as unpadded base64url UTF-8, join with `/`, and prepend `/` when segments exist;
zero segments represent the empty pointer. Raw RFC 6901 escapes and empty segments are preserved.
Pointer-prefixed issue messages use `json_pointer_diagnostic` with that pointer and an exact `suffix`.
This preserves diagnostics without mistaking them for storage paths; public error envelopes stay unchanged.
Activity-history read receipts retain immutable event IDs/checksums, metadata and pagination instead
of recursively copying previously archived payloads. Their originals remain durable and the direct
MCP response still returns those full original records; other tools retain their sanitized outputs.

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
