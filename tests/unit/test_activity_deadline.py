"""Archive cold paths fit a bounded deadline without retrying the write."""
from types import SimpleNamespace

import pytest

from finplan_tools.core.errors import ToolError
from finplan_tools.handler import invoke
from tests.unit.test_portfolio_lifecycle import PD, setup


@pytest.mark.parametrize("remaining_ms, expected", [(28000, 20.0), (5500, 4.5), (800, None)])
def test_archive_deadline_respects_actual_lambda_time_and_fails_closed(offline, remaining_ms, expected):
    platform = setup(offline)
    offline.runtime.durable_activity_receipts = True
    deadlines = []

    def client(timeout):
        deadlines.append(timeout)
        # Emulate the measured 10.95-second cold archive path, without sleep.
        if len(deadlines) == 2 and timeout < 10.95:
            raise ToolError.dependency_unavailable("archive transport deadline exceeded", producer="financialplanning")
        return platform

    offline.runtime.platform = client
    context = SimpleNamespace(get_remaining_time_in_millis=lambda: remaining_ms)
    result = invoke("get_portfolio_decision", offline.event({"decision_id": PD}), context, offline.runtime)
    assert deadlines[0] == 30  # Existing caller/tool timeout is unchanged.
    if expected is None:
        assert len(deadlines) == 1
    else:
        assert deadlines == [30, expected]
    receipts = [row for row in platform.calls if row[0] == "record_agent_activity"]
    if expected == 20:
        assert "code" not in result and len(receipts) == 1
        assert receipts[0][1][0]["idempotency_key"].startswith("receipt-")
    else:
        assert result["code"] == "DEPENDENCY_UNAVAILABLE" and receipts == []
    # Exactly one persistence attempt: a timeout never blindly repeats a write.
    assert len(deadlines) <= 2
