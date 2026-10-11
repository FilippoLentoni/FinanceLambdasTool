"""Beta: real traditional inference, immutable retrieval and CI spend denial; no paid jobs."""
from __future__ import annotations

import pytest

from tests.deployed_support import (
    TARGET_ENV,
    deployed,
    invoke,
    is_error,
    validate_result,
)

pytestmark = [pytest.mark.deployed, deployed, pytest.mark.skipif(TARGET_ENV != 'beta', reason='classical acceptance is beta-only')]


def call(tool, arguments):
    doc=invoke(tool,arguments)
    validate_result(tool,doc)
    assert not is_error(doc),doc
    return doc


def test_frozen_classical_recommendation_explanation_and_retrieval():
    plan=call('recommend_classical_portfolio',{'algorithm':'min_variance'})
    rec=plan['recommendation']
    assert rec['strategy']=='min_variance'
    assert rec['portfolio_state']['source']=='saved_paper'
    assert abs(sum(row['weight'] for row in rec['target_weights'])+rec['cash_weight']-1)<1e-7
    assert rec['snapshot_checksum'].startswith('sha256:')
    assert all({'current_quantity','target_quantity','delta_quantity','reference_price','price_as_of'} <= set(row) for row in rec['decisions'])
    fetched=call('get_classical_analysis',{'analysis_id':plan['analysis_id']})
    assert fetched==plan
    why=call('explain_classical_recommendation',{'analysis_id':plan['analysis_id'],'instrument_id':'GOOGL'})
    assert why['analysis_kind']=='explanation'
    assert why['source_analysis_id']==plan['analysis_id']
    assert why['source_analysis_ref']==plan['analysis_ref']
    listing=call('list_classical_analyses',{'portfolio_id':rec['portfolio_state']['portfolio_id'],'kind':'recommendation','limit':100})
    assert plan['analysis_id'] in {row['analysis_id'] for row in listing['analyses']}


def test_ci_paid_research_is_denied_before_model_call():
    doc=invoke('run_portfolio_research',{'review_id':'ca_'+'1'*32,'dry_run':False,'confirmed_by_user':True,'idempotency_key':'ci-paid-denial'})
    validate_result('run_portfolio_research',doc)
    assert is_error(doc) and doc['code']=='FORBIDDEN',doc


def test_classical_input_is_strict_and_does_not_accept_ppo():
    doc=invoke('recommend_classical_portfolio',{'algorithm':'ppo'})
    validate_result('recommend_classical_portfolio',doc)
    assert is_error(doc) and doc['code']=='VALIDATION_FAILED',doc
