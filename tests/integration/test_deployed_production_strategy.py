"""PST-05 (tasks 2.1/2.2; beta and gamma, deployed). REAL direct invocations of the deployed
``production_strategy`` tool as the pipeline test principal (``ci_test``, acting for the project
owner), which makes real SigV4 calls to the same environment's FinanceModel selection operation.

Until FinanceModel's 1.1.0 release (``add-daily-recommendation-and-on-demand-experiments``) serves
``GET``/``PUT /v1/production-strategy`` in the environment, every call that reaches the dependency
gate must answer ``DEPENDENCY_UNAVAILABLE`` (never a wiring failure); the tests assert exactly that
and do not skip. Once it is released, the full PST-05 sequence runs: get, set of an unevaluated
strategy (``no_evaluation_evidence``), confirmed set of ``buy_and_hold``, idempotent repeat, clear.
A selection that existed before the test is restored afterwards. Skipped offline and in prod (prod is
Gateway-only for state-changing tools; the smoke suite checks the smoke role cannot invoke it).
"""

from __future__ import annotations

import os
import uuid

import pytest

from tests.deployed_support import TARGET_ENV, WIRING_FAILURES, deployed, invoke, is_error, validate_result

pytestmark = [pytest.mark.deployed, deployed, pytest.mark.skipif(TARGET_ENV == "prod", reason="prod: state-changing tools are Gateway-only")]

TOOL = "production_strategy"
#: A registered strategy without evaluation evidence in this environment's FinanceModel registry.
UNEVALUATED = os.environ.get("FINPLAN_PST_UNEVALUATED_STRATEGY", "momentum_12_1")
RUN = uuid.uuid4().hex[:12]


def call(arguments):
    doc = invoke(TOOL, arguments)
    validate_result(TOOL, doc)
    if is_error(doc):
        assert doc["code"] not in WIRING_FAILURES, doc
    return doc


def unavailable(doc) -> bool:
    return is_error(doc) and doc["code"] == "DEPENDENCY_UNAVAILABLE"


def key(step: str) -> str:
    return f"ci-pst05-{TARGET_ENV}-{RUN}-{step}"


def test_pst05_confirmation_is_checked_before_financemodel():
    """Missing confirmation fails before any FinanceModel call, whether or not it is released."""
    doc = invoke(TOOL, {"action": "set", "strategy_id": "buy_and_hold", "idempotency_key": key("noconfirm"), "synthetic": True})
    validate_result(TOOL, doc)
    assert is_error(doc) and doc["code"] == "PRECONDITION_FAILED" and doc["details"]["reason"] == "confirmation_required", doc


def test_pst05_sequence():
    first = call({"action": "get", "synthetic": True})
    if unavailable(first):
        # FinanceModel strategy API absent: every action answers DEPENDENCY_UNAVAILABLE, not retryable
        assert first["details"].get("reason") in ("operation_not_released", "producer_not_released", "route_not_deployed"), first
        for req in (
            {"action": "set", "strategy_id": "buy_and_hold", "idempotency_key": key("set-unavail"), "confirmed_by_user": True, "synthetic": True},
            {"action": "clear", "idempotency_key": key("clear-unavail"), "confirmed_by_user": True, "synthetic": True},
        ):
            doc = call(req)
            assert unavailable(doc), doc
        return
    # 1. get returns none or the current value
    assert not is_error(first), first
    assert first["environment"] == TARGET_ENV and first["action"] == "get" and first["changed"] is False
    original = first["strategy"]
    try:
        # 2. set of an unevaluated strategy -> FinanceModel's rejection passed through
        bad = call({"action": "set", "strategy_id": UNEVALUATED, "idempotency_key": key("unevaluated"), "confirmed_by_user": True, "synthetic": True})
        # FinanceModel names the first failing eligibility rule under details.rule: an unknown strategy
        # is "strategy_not_registered"; a known one without a universe benchmark is
        # "no_evaluation_evidence". Either way it must be refused and nothing selected.
        rule = bad.get("details", {}).get("rule") or bad.get("details", {}).get("reason")
        assert is_error(bad) and bad["code"] == "VALIDATION_FAILED" and rule in ("no_evaluation_evidence", "strategy_not_registered"), bad
        # 3. confirmed set of buy_and_hold; FinanceModel get shows it
        set_req = {"action": "set", "strategy_id": "buy_and_hold", "idempotency_key": key("set"), "confirmed_by_user": True, "synthetic": True}
        done = call(set_req)
        if is_error(done) and (done.get("details") or {}).get("rule") == "no_evaluation_evidence":
            # Selection needs a succeeded universe benchmark in this environment; benchmarks run only
            # when the user starts one (never scheduled), so a fresh environment has none yet.
            pytest.skip(f"no universe benchmark has been run in {TARGET_ENV} yet; the set/get/clear steps need one")
        assert not is_error(done) and done["strategy"]["strategy_id"] == "buy_and_hold", done
        got = call({"action": "get"})
        assert not is_error(got) and got["strategy"]["strategy_id"] == "buy_and_hold", got
        # 4. a repeat with the same key returns the original result
        assert call(set_req) == done
        # 5. clear -> get returns none
        cleared = call({"action": "clear", "idempotency_key": key("clear"), "confirmed_by_user": True, "synthetic": True})
        assert not is_error(cleared) and cleared["strategy"] is None, cleared
        after = call({"action": "get"})
        assert not is_error(after) and after["strategy"] is None, after
    finally:
        if original is not None:  # leave the environment as it was found
            call({"action": "set", "strategy_id": original["strategy_id"], "idempotency_key": key("restore"), "confirmed_by_user": True, "synthetic": True})
