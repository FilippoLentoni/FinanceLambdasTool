"""TEST-ONLY package: in-process mock producers, synthetic scenarios and offline runtimes.

Never part of the deployable artifact: the wheel packages ``src/finplan_tools`` only, and the build
check ``scripts/check_artifact.py`` fails a Lambda bundle that contains this package or any
fixture-minting code (spec tool-environment-wiring, ENVW-05).

* :mod:`.mock_platform` - FinancialPlanning plan/ingestion API double (plans, versions,
  publications, portfolios, snapshots, observations, ingestion) with idempotency and conflict
  semantics and fault injection.
* :mod:`.mock_jobs` - FinanceModel job API double (submit with dry run, status, result) with
  idempotency, budget categories, approval states and outcome control.
* :mod:`.params` - an SSM ``get_parameter`` double.
* :mod:`.scenarios` - synthetic scenario data (S&P 500 tracking-ETF daily dataset, synthetic
  portfolios) built from the pinned contract package's own fixtures.
* :mod:`.runtime` - :func:`offline_runtime` wires the mocks into a pipeline ``Runtime``.
"""
