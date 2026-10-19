"""Producer gateway 5xx retry (D6/D7), request size limit and the archive payload (record_agent_activity)."""
from __future__ import annotations

import logging

import pytest

from finplan_tools.backends.platform import MAX_ATTEMPTS, PlatformClient, call
from finplan_tools.core.config import ToolLimits, tool_limits_problems
from finplan_tools.core.errors import ToolError
from finplan_tools.core.transport import CallMeta, ProducerResponse
from finplan_tools.handler import invoke
from tests.unit.test_portfolio_lifecycle import setup

CID = 'lt-' + 'a' * 32
META = CallMeta(CID, '1.14.0')
GATEWAY_5XX = {'code': 'INTERNAL', 'message': 'internal error', 'retryable': False, 'details': {},
               'correlation_id': '5f0c7a8e-2b4d-4e7a-9c1b-0d2e3f4a5b6c', 'contract_version': '1.13.1'}


class Transport:
    def __init__(self, *responses):
        self.responses, self.requests = list(responses), []

    def send(self, request):
        self.requests.append(request)
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def ok(body=None):
    return ProducerResponse(200, body if body is not None else {'ok': True})


def gateway_5xx(status=500):
    return ProducerResponse(status, dict(GATEWAY_5XX))


def test_gateway_5xx_of_a_read_is_retried_with_backoff(no_retry_sleep):
    t = Transport(gateway_5xx(), gateway_5xx(504), ok())
    assert call(t, 'financialplanning', 'GET', 'v1/plans/x/publications', META) == {'ok': True}
    assert len(t.requests) == 3 and len(no_retry_sleep) == 2 and no_retry_sleep[0] < no_retry_sleep[1]


def test_persistent_gateway_5xx_is_retryable_dependency_unavailable_not_internal(caplog):
    t = Transport(*[gateway_5xx() for _ in range(MAX_ATTEMPTS)])
    with caplog.at_level(logging.WARNING), pytest.raises(ToolError) as err:
        call(t, 'financialplanning', 'GET', 'v1/publications/x/executions', META)
    assert err.value.code == 'DEPENDENCY_UNAVAILABLE' and err.value.retryable is True
    assert err.value.details['reason'] == 'producer_gateway_error' and err.value.details['attempts'] == MAX_ATTEMPTS
    assert len(t.requests) == MAX_ATTEMPTS and CID in caplog.text


def test_producer_internal_error_is_not_retried_and_is_logged(caplog):
    own = {**GATEWAY_5XX, 'correlation_id': CID}
    t = Transport(ProducerResponse(500, own))
    with caplog.at_level(logging.WARNING), pytest.raises(ToolError) as err:
        call(t, 'financialplanning', 'GET', 'v1/plans/x', META)
    assert err.value.code == 'INTERNAL' and len(t.requests) == 1
    assert 'returned INTERNAL' in caplog.text


def test_write_without_idempotency_key_is_never_replayed():
    t = Transport(gateway_5xx())
    with pytest.raises(ToolError) as err:
        call(t, 'financialplanning', 'POST', 'v1/plans/x/versions', META, body={'a': 1})
    assert err.value.code == 'DEPENDENCY_UNAVAILABLE' and len(t.requests) == 1


@pytest.mark.parametrize('first', [
    ProducerResponse(429, {'code': 'RATE_LIMITED', 'message': 'the API throttled the request; retry later', 'retryable': True, 'details': {}, 'correlation_id': 'x', 'contract_version': '1.13.1'}),
    ProducerResponse(503, None),
    ToolError.dependency_unavailable('the financialplanning API is unreachable', producer='financialplanning'),
    gateway_5xx(),
])
def test_activity_archive_retries_every_transient_failure(first):
    t = Transport(first, ok({'activity_event_id': 'ae_1'}))
    client = PlatformClient(t)
    body = {'event_kind': 'tool_invocation', 'correlation_id': CID, 'payload': {}, 'idempotency_key': 'receipt-1'}
    assert client.record_agent_activity(body, META) == {'activity_event_id': 'ae_1'}
    assert len(t.requests) == 2 and t.requests[0].body == t.requests[1].body


def test_caller_visible_throttling_still_passes_through_once():
    t = Transport(ProducerResponse(429, None))
    with pytest.raises(ToolError) as err:
        call(t, 'financialplanning', 'GET', 'v1/snapshots', META)
    assert err.value.code == 'RATE_LIMITED' and len(t.requests) == 1


def test_request_limit_defaults_to_four_mebibytes_and_is_bounded():
    assert ToolLimits.from_document(None).request_max_bytes == 4 * 1024 * 1024
    assert ToolLimits.from_document({'request_max_bytes': 2048}).request_max_bytes == 2048
    doc = {**ToolLimits.from_document(None).raw, 'request_max_bytes': 7_000_000}
    assert any('request_max_bytes' in p for p in tool_limits_problems(doc))


def archive(offline, payload, key='archive-limit'):
    platform = setup(offline)
    args = {'event_kind': 'tool_call', 'correlation_id': 'corr-archive-limit', 'idempotency_key': key, 'payload': payload}
    return platform, invoke('record_agent_activity', offline.event(args), None, offline.runtime)


def test_large_archive_payload_below_the_limit_is_recorded(offline):
    payload = {'arguments': {'prompt': 'x' * 3_000_000}}
    platform, doc = archive(offline, payload)
    assert doc.get('activity_event_id'), doc
    assert platform.calls[0][1][0]['payload'] == payload


def test_request_above_the_limit_is_a_typed_validation_error(offline):
    platform, doc = archive(offline, {'blob': 'x' * (4 * 1024 * 1024)})
    assert doc['code'] == 'VALIDATION_FAILED' and doc['details']['reason'] == 'request_too_large'
    assert not platform.calls


def test_archive_payload_text_that_looks_like_a_path_is_evidence_not_a_location(offline):
    # The 2026-10-19 planner-input capture was refused by the contract's path-like-value check.
    payload = {'pointer_text': '/holdings/weights', 'regex': 'use \\d+ to match', 'home': '~/notes', 'relative': '../x'}
    platform, doc = archive(offline, payload)
    assert doc.get('activity_event_id'), doc
    assert platform.calls[0][1][0]['payload'] == payload


def test_archive_metadata_fields_keep_the_full_storage_check(offline):
    platform = setup(offline)
    args = {'event_kind': '/etc/passwd', 'correlation_id': 'corr-archive-limit', 'idempotency_key': 'archive-meta', 'payload': {}}
    doc = invoke('record_agent_activity', offline.event(args), None, offline.runtime)
    assert doc['code'] == 'VALIDATION_FAILED' and not platform.calls


def test_internal_tool_errors_are_logged_with_their_cause(offline, caplog):
    platform = setup(offline)
    platform.error = ToolError.internal('the producer returned an inconsistent document', reason='diagnostic_test')
    with caplog.at_level(logging.INFO):
        doc = invoke('get_portfolio_decision', offline.event({'decision_id': 'pd_01M57J3YJ5K08TAV8Q1KXT72BP'}), None, offline.runtime)
    assert doc['code'] == 'INTERNAL'
    record = next(r for r in caplog.records if r.getMessage().startswith('tool error INTERNAL'))
    assert record.levelno == logging.ERROR and record.exc_info is not None
    assert 'inconsistent document' in record.getMessage() and 'diagnostic_test' in record.getMessage()
