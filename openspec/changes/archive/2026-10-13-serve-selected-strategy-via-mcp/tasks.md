# Tasks

## 1. Implementation and local verification

- [x] 1.1 Implement strategy-neutral direct Lambda invocation with scoped reference validation, caller/correlation metadata and no automatic retry; verify successful, denied, corrupt and cross-environment responses.
- [x] 1.2 Configure the recommendation target for 300 seconds and backend invocation for 280 seconds, with narrow reader-role InvokeFunction permission; verify synthesized templates and timeout tests.
- [x] 1.3 Document the read-only recommendation tool and verify its request/response contracts and existing adapter safety tests.

## 2. Deployed beta verification

- [x] 2.1 Deploy beta through the existing pipeline and verify the registered Gateway Lambda target returns the pinned strategy under an authenticated caller.

## Workflow follow-up

- Review before archiving; existing explanation and gamma/prod tasks retain their own acceptance criteria.
