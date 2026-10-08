# Direct invocation

Before the FinanceAgent Gateway exists, a tool Lambda can be invoked directly. Two callers may do
this: the single direct-test principal of the environment (the project owner) and this
repository's pipeline test and smoke roles.

## Who may invoke

The Lambda resource policy is the security boundary. The direct-test principal is named, never by
ARN, in the SSM parameter `/finplan/<env>/financelambdastool/config/direct-test-principal-name`.
The bootstrap writes that parameter from local, untracked configuration. In prod, the direct-test
grant covers read-only tools only. The pipeline also refuses state-changing tools in prod for
direct and pipeline callers.

## Event shape

```json
{
  "finplan_invocation": {
    "source": "direct_test",
    "environment": "beta",
    "correlation_id": "owner-check-0001",
    "contract_version": "1.0.0"
  },
  "tool": "get_plan_version",
  "arguments": { "plan_version_id": "pv_<ULID>" }
}
```

| Field | Meaning |
|---|---|
| `finplan_invocation.source` | `direct_test` (the project owner) or `ci_test` (pipeline suites). It selects the identity class (`direct:<env>` or `pipeline:<env>`) and grants nothing by itself. |
| `finplan_invocation.environment` | Optional. Any value other than the function's own environment returns `FORBIDDEN`. |
| `finplan_invocation.correlation_id` | Optional, `[A-Za-z0-9][A-Za-z0-9_-]{7,127}`. If absent, one is generated. |
| `finplan_invocation.contract_version` | Optional. It may also appear as `arguments.contract_version`. A major the release does not serve returns `UNSUPPORTED_CONTRACT_VERSION`. |
| `tool` | Optional. A name other than the function's own tool returns `VALIDATION_FAILED`. |
| `arguments` | The tool request. It must validate against the pinned `tools/<tool>-request` schema. |

A `caller` field is never trusted, wherever it appears. It is removed and logged only as a
redacted annotation. An event without a recognised source returns `UNAUTHORIZED`.

## Result

On success, the result is the tool response document, which validates against the pinned
`tools/<tool>-response` schema. On failure, it is the contract error envelope with `code`,
`message`, `retryable`, `details`, `correlation_id` and `contract_version`. The function never
raises.

## Offline run

This runs the same pipeline against the in-process mock producers, with no credentials:

```bash
uv run python - <<'PY'
import json, sys
sys.path[:0] = ["src", "testing"]
from finplan_tools.handler import invoke
from finplan_tools_testing.runtime import offline_runtime

o = offline_runtime("beta")
_, pv = o.platform.add_plan(o.platform.add_portfolio())
print(json.dumps(invoke("get_plan_version", o.event({"plan_version_id": pv}), None, o.runtime), indent=2))
PY
```

## Deployed invocation (beta and gamma)

Run this as the environment's direct-test principal, the project owner. The bootstrap writes that
principal's name to `/finplan/<env>/financelambdastool/config/direct-test-principal-name`, and the
pre-deploy step turns it into the invoke grant. The command reads the tool's published reference
from SSM, so no function name, ARN or account appears here:

```bash
ENV=beta
TOOL=get-plan-version     # the kebab-case tool name
REF=$(aws ssm get-parameter --name "/finplan/$ENV/financelambdastool/lambda/$TOOL-arn" \
      --query Parameter.Value --output text)
aws lambda invoke --function-name "$REF" --cli-binary-format raw-in-base64-out \
  --payload '{"finplan_invocation": {"source": "direct_test", "environment": "'"$ENV"'"},
              "tool": "get_plan_version",
              "arguments": {"plan_version_id": "<a plan_version_id from the beta platform fixtures>"}}' \
  /dev/stdout
```

The reference is alias-qualified (`:current`), and the grant exists only on that alias. Expect
these results:

- With a known fixture ID, the response validates against `tools/get-plan-version-response`.
- With an unknown ID, the result is the `NOT_FOUND` envelope.
- If the call returns `AccessDeniedException`, either no direct-test principal is configured for that
  environment or you are not that principal.

In prod, the same command works for read-only tools only. The prod grant does not cover
state-changing tools, so `publish_plan_version` and the other write tools are denied before they
run. After you change the direct-test principal name, redeploy the current release (pipeline
variable `rollback_to_release_id` set to the current release ID). The grant changes; nothing is
rebuilt.
