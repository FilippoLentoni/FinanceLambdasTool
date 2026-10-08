# Pipeline bootstrap (one time)

The FinanceLambdasTool pipeline is created once, by the project owner, with their existing
authenticated AWS CLI session. The user approved this bootstrap in principle on 2026-10-07. It runs
only after this IaC is implemented, and it always shows the exact stacks and a cost estimate first.
Nothing in this repository runs it automatically, and it is never run during spec work.

## What it creates

| Stack | Contents |
|---|---|
| `finplan-shared-financelambdastool-pipeline-store` | The pipeline store bucket (artifacts, content-addressed Lambda assets, release ledger, staged tooling template). Versioned, TLS only, retained on deletion. Deployed inline (`LegacyStackSynthesizer`). |
| `finplan-shared-financelambdastool-tooling` | The CodePipeline V2 pipeline, its CodeBuild projects and the scoped roles: the account-level pipeline and build roles, and per environment the deploy role, the CloudFormation execution role and the stage (pre-deploy, publish, test) role. Staged in the store with the operator's credentials (`CliCredentialsStackSynthesizer`). |

Neither stack references the CDK bootstrap (`CDKToolkit`, its roles or its asset buckets); the
bootstrap refuses an assembly that does. The three environment stacks
(`finplan-<env>-financelambdastool-tools`) are **not** created by the bootstrap: the pipeline
deploys them later, after its source-stage dry run has passed.

It creates no budget, no permission boundary and no CodeConnection. FinancialPlanning owns the
USD 50 project budget, its deny action and the boundaries. The CodeConnection is the existing one
the platform already uses.

## Before you run it

1. FinancialPlanning is bootstrapped (the boundaries `finplan-<env>-permission-boundary` and
   `finplan-shared-permission-boundary` exist, and so does
   `/finplan/shared/financialplanning/config/codeconnection-ref`).
2. The local, untracked configuration exists. The bootstrap reads it the same way as the other
   repositories: `--config PATH`, else `$FINPLAN_BOOTSTRAP_CONFIG`, else `~/.finplan/bootstrap.json`
   (account and primary region; the connection is optional because the platform's is reused),
   plus the optional overlay `~/.finplan/financelambdastool-bootstrap.json`:

   ```json
   {"direct_test_principal_name": "<iam-role-name>"}
   ```

   The value is the IAM principal **name** of the project owner (`<name>`, `role/<name>` or
   `user/<name>`), never an ARN. The account root, wildcards, account IDs and lists are refused. A
   per-environment map `direct_test_principal_names` (`{"beta": "...", "gamma": "..."}`) is also
   accepted. Without a name, no direct-test grant exists anywhere (fail closed) and only the
   pipeline suites can invoke the tools. A configuration file inside the repository is refused.
3. Synthesize: `uv run python scripts/synth.py` (offline; it writes `cdk.out/`).

## Run

```bash
uv run python scripts/bootstrap.py
```

The sequence (it stops at the first failing step; every step before the confirmation is read-only):

1. Copies only the two account-level stacks into `cdk.out.bootstrap/` and refuses CDK-bootstrap references.
2. Prints the exact stacks with their resource types and a monthly cost estimate from the AWS Price
   List API (no price is stored in this repository).
3. Checks the STS account and region against the local configuration. A root caller is accepted; the
   bootstrap then prints the recommendation to move the operator to a scoped, MFA-protected role.
4. Checks that the reused CodeConnection is `AVAILABLE` and that every deploy action uses a scoped
   deploy role declared by these stacks.
5. Asks you to type `deploy`. Anything else stops with nothing changed.
6. Writes `/finplan/shared/financelambdastool/config/codeconnection-ref`, deploys the two stacks, then:
   - creates `/aws/codebuild/<project>` for the four CodeBuild projects with the cost tags and sets
     30-day retention (the ownership matrix does not list CodeBuild log groups for this repository
     yet, so they are not declared in IaC; reported as a contract gap);
   - writes the default `/finplan/<env>/financelambdastool/config/tool-limits` for each environment,
     **unless it is present** (your edits are kept);
   - writes `/finplan/<env>/financelambdastool/config/direct-test-principal-name` for each configured
     environment (the value is never printed);
   - writes `/finplan/shared/financelambdastool/config/budget-enforced-role-names` (the account-level
     pipeline and build roles) for the FinancialPlanning budget action.
7. Runs the source-stage dry run: one execution must fetch `main` of
   `FilippoLentoni/FinanceLambdasTool`. Then the transition into Build is enabled. If the
   connection cannot read the repository, the deploy stages stay disabled and the bootstrap asks you
   to extend the GitHub App installation of the connection to this repository and rerun.

## After it

- The pipeline's first run deploys beta. Each environment stage first runs `PreDeploy`: the contract
  pin must be allowed there (0.x is beta-only), the platform release in that environment must serve
  the pinned major (FinanceModel is optional), the per-call tool limits must stay within
  `per_call_max_fraction` of the shared budget allocation, and that environment's producer
  endpoints, Gateway principal and direct-test principal name are resolved from its own SSM.
- Configuration changes such as a new direct-test principal, the FinanceAgent Gateway principal or
  a FinanceModel release take effect by redeploying the same release: start the pipeline with
  `rollback_to_release_id` set to the current release ID. Nothing is rebuilt.
- Rollback: start the pipeline with `rollback_to_release_id` set to a recorded release ID. Its
  stored build output is re-emitted after its digest is re-verified, and the prod manifest records
  `rolled_back_from`.

## Cost

Standing cost is near zero: the Lambdas are on demand, with no provisioned concurrency, VPC, NAT or
endpoints. CodeBuild and CodePipeline bill per use, and the store holds small, expiring objects. The
pre-run estimate is computed at run time from the Price List API. The tool catalog parameter uses
the SSM standard tier while it stays under 4 KB; a longer catalog is written to the advanced tier
(billed monthly).
