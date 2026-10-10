"""Public dispatcher privacy for accepted paper decisions and immutable analysis reads."""
from __future__ import annotations

import copy
import hashlib
import json
from types import SimpleNamespace

import pytest

from finplan_tools.core.artifacts import response_leaks
from finplan_tools.core.contracts import validate_document
from finplan_tools.handler import invoke
from tests.unit.test_classical_tools import Client, wire
from tests.unit.test_portfolio_lifecycle import PD, PF, context, fixture, setup


def platform_caller():
    # The real Platform Caller.audit_view, with a realistic private transport principal.
    account = '1' * 12
    principal = ':'.join(('arn', 'aws', 'sts', '', account, 'assumed-role/paper-writer/session'))
    return {'principal': principal, 'role_class': 'plan-writer', 'channel': 'hosted_agent',
            'on_behalf_of': {'subject_hash': 'sha256:' + 'ab' * 32, 'roles': ['viewer'],
                             'channel': 'hosted_agent', 'correlation_id': 'corr-accepted-paper'},
            'account_id': account, 'transport_principal': principal}


def resolution():
    doc = fixture('resolve_portfolio_decision')
    doc.update(caller=platform_caller(), checksum='sha256:' + 'cd' * 32,
               execution_mode='historical_reference_simulation')
    return doc


def without_caller(value):
    if isinstance(value, dict):
        return {key: without_caller(item) for key, item in value.items() if key != 'caller'}
    if isinstance(value, list):
        return [without_caller(item) for item in value]
    return value


def assert_public(result, source):
    assert 'code' not in result, result
    assert not response_leaks(result)
    assert without_caller(result) == without_caller(source)
    rendered = json.dumps(result)
    assert platform_caller()['principal'] not in rendered
    assert platform_caller()['account_id'] not in rendered
    assert 'account_id' not in rendered and 'transport_principal' not in rendered


@pytest.mark.parametrize('gateway', ['primary', 'classical'])
def test_resolved_decision_and_exact_retry_are_public_without_mutating_stored_evidence(offline, gateway):
    platform = setup(offline)
    offline.runtime.gateway_research_authorizer = SimpleNamespace(verify_context=lambda _: SimpleNamespace(identity='cognito:verified-user-hash', groups=frozenset({'viewer'})))
    offline.runtime.durable_activity_receipts = True
    source = resolution()
    original = copy.deepcopy(source)
    calls = []

    def resolve(decision_id, body, meta):
        calls.append((decision_id, copy.deepcopy(body), meta))
        return source  # Same immutable object on idempotent replay.

    platform.resolve_portfolio_decision = resolve
    args = {'decision_id': PD, 'action': 'accept', 'expected_revision': 1, 'confirmed_by_user': True, 'idempotency_key': 'reviewed-acceptance'}
    first = invoke('resolve_portfolio_decision', args, context('resolve_portfolio_decision', gateway=gateway), offline.runtime)
    second = invoke('resolve_portfolio_decision', args, context('resolve_portfolio_decision', gateway=gateway), offline.runtime)
    assert first == second
    assert_public(first, source)
    assert validate_document(first, 'tools/resolve-portfolio-decision-response').valid
    assert first['caller'] == {'principal_hash': 'sha256:' + hashlib.sha256(source['caller']['principal'].encode()).hexdigest(),
                               'role_class': source['caller']['role_class'], 'channel': source['caller']['channel'],
                               'on_behalf_of': source['caller']['on_behalf_of']}
    assert source == original
    assert calls[0][1] == calls[1][1] and calls[0][1]['idempotency_key'].startswith('lt_')
    assert platform.calls[-1][1][0]['payload']['response'] == first


@pytest.mark.parametrize('tool', ['get_portfolio_decision', 'list_portfolio_decisions', 'get_portfolio_history', 'list_agent_activity'])
def test_accepted_decision_history_and_activity_read_project_nested_resolution_callers(offline, tool):
    platform = setup(offline)
    source = fixture(tool)
    if tool == 'get_portfolio_decision':
        source['decision'].update(status='accepted', resolution=resolution())
    elif tool == 'list_portfolio_decisions':
        source['decisions'][0].update(status='accepted', resolution=resolution())
    elif tool == 'get_portfolio_history':
        source['history'][0]['resolution'] = resolution()
    else:
        source['events'][0].update(caller=platform_caller(), payload={'resolution': resolution(), 'narrative': 'Accepted paper plan'})
    original = copy.deepcopy(source)
    setattr(platform, tool, lambda *args, **kwargs: source)
    request = {'decision_id': PD} if tool == 'get_portfolio_decision' else {'portfolio_id': PF}
    result = invoke(tool, offline.event(request), None, offline.runtime)
    assert_public(result, source)
    assert validate_document(result, 'tools/' + tool.replace('_', '-') + '-response').valid
    assert source == original


@pytest.mark.parametrize('tool', ['explain_portfolio_decision', 'compare_portfolio_decisions', 'evaluate_portfolio_decision', 'get_classical_analysis'])
def test_analysis_dispatcher_projects_accepted_resolution_evidence_and_retrieval(offline, tool):
    setup(offline)
    source = fixture('explain_portfolio_decision')
    if tool == 'compare_portfolio_decisions':
        source.update(analysis_kind='comparison', previous_resolution=resolution(), current_resolution=resolution())
        request = {'previous_decision_id': PD, 'current_decision_id': PD[:-1] + 'S'}
    elif tool == 'evaluate_portfolio_decision':
        source.update(analysis_kind='performance', paper_execution_evidence=[{'decision_id': PD, 'resolution': resolution()}],
                      observed_paper={'status': 'available', 'pnl': -2.45, 'recorded_paper_costs': .02}, gap={'total': -.05})
        request = {'decision_id': PD}
    else:
        source.update(resolution=resolution(), decision_status='accepted')
        request = {'analysis_id': source['analysis_id']} if tool == 'get_classical_analysis' else {'decision_id': PD}
    original = copy.deepcopy(source)
    client = Client(source)
    wire(offline, client)
    result = invoke(tool, offline.event(request), None, offline.runtime)
    assert_public(result, source)
    assert validate_document(result, 'tools/' + tool.replace('_', '-') + '-response').valid
    assert source == original and len(client.calls) == 1
    assert result['analysis_ref'] == source['analysis_ref']


def test_public_projection_does_not_disable_the_guard_for_other_leaking_fields(offline):
    platform = setup(offline)
    offline.runtime.gateway_research_authorizer = SimpleNamespace(verify_context=lambda _: SimpleNamespace(identity='cognito:verified-user-hash', groups=frozenset({'viewer'})))
    source = resolution()
    source['reason'] = 'Unexpected private location ' + platform_caller()['principal']
    platform.resolve_portfolio_decision = lambda *args, **kwargs: source
    args = {'decision_id': PD, 'action': 'accept', 'expected_revision': 1, 'confirmed_by_user': True, 'idempotency_key': 'guard-still-enabled'}
    result = invoke('resolve_portfolio_decision', args, context('resolve_portfolio_decision'), offline.runtime)
    assert result['code'] == 'INTERNAL'
    assert not response_leaks(result)
