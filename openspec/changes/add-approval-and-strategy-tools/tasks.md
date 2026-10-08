# Tasks

Test IDs are defined in the mapping table at the end. CI uses the in-process mock backend. **Deployed** tests invoke the deployed Lambda as the single direct-test principal (project owner) in beta or gamma.

## 1. Tool

- [x] 1.1 Pin contracts 1.1.0 and add the `production_strategy` catalog entry. Verify that the catalog contract test passes and the schema `$id` matches 1.1.0.
- [x] 1.2 Implement the handler (get/set/clear, confirmation, group check, derived idempotency key, error pass-through). Verify PST-01 to PST-03 unit tests with the mock backend: `none` on get; missing confirmation and wrong group make zero downstream calls; `no_evaluation_evidence` is passed through.
- [x] 1.3 Grant the role the FinanceModel selection operations with an explicit deny on `ssm:PutParameter` for FinanceModel config. Verify the PST-04 IAM policy simulation.
- [x] 1.4 Document the tool in `docs/tools.md`. Verify that the documented example request validates against the schema.

## 2. Deployed verification

- [ ] 2.1 **PST-05 (beta, deployed):** invoke the deployed tool:
  1. `get` returns `none` or the current value.
  2. `set` of an unevaluated strategy returns `no_evaluation_evidence`.
  3. `set` of `buy_and_hold` with confirmation succeeds, and FinanceModel `get` shows it.
  4. A repeat with the same key returns the original result.
  5. `clear` makes `get` return `none`.
- [ ] 2.2 Repeat PST-05 in gamma. Prod smoke: `get` only (non-mutating).

## Requirement-to-test mapping

| Capability | Requirement | Test ID | Type |
|---|---|---|---|
| production-strategy-tool | Read the production strategy | PST-01, PST-05 | unit + **beta/gamma/prod deployed** |
| production-strategy-tool | Set and clear are confirmed user actions | PST-02, PST-05 | unit + **beta/gamma deployed** |
| production-strategy-tool | FinanceModel decides validity | PST-03, PST-05 | unit + **beta/gamma deployed** |
| production-strategy-tool | No side doors | PST-04 | policy sim |

## Workflow follow-up

- Archive after the gamma deployed test and the prod smoke pass.
