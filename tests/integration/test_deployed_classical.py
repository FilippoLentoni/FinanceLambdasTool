"""Beta: real min-variance/CVaR/PPO recommendations over an explicit universe, immutable retrieval
and CI spend denial; no paid jobs."""
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


#: The beta modeling competition's universe (beta-competition-v1).
EIGHT=['AAPL','AMZN','GOOGL','META','MSFT','NVDA','TSLA','VOO']
MV='generate_portfolio_recommendation_min_variance'
CVAR='generate_portfolio_recommendation_cvar'
PPO='generate_portfolio_recommendation_ppo'
#: Acceptable typed refusals of a universe the producer cannot serve (never wiring/permission failures).
UNIVERSE_REFUSALS=('NOT_FOUND','PRECONDITION_FAILED','VALIDATION_FAILED','CONFLICT')


@pytest.fixture(scope='module')
def scenario():
    """An isolated all-cash scenario on the latest approved actual market snapshot (never the saved book)."""
    listing=call('list_market_snapshots',{})
    snap=next((s for s in listing['snapshots'] if s['status']=='approved' and not s.get('synthetic')),None)
    assert snap is not None,listing
    market=call('query_market_data',{'input_snapshot_id':snap['input_snapshot_id'],'instrument_ids':EIGHT})
    rows={row['instrument_id']:row for row in market['instruments']}
    assert set(EIGHT)<=set(rows),sorted(rows)
    as_of=max(rows[i]['last_date'] for i in EIGHT)
    return {'input_snapshot_id':snap['input_snapshot_id'],'as_of':as_of,'instruments':list(EIGHT),
            'holdings':{'weights':[{'instrument_id':i,'weight':0} for i in EIGHT],'cash_weight':1,'portfolio_value':100000,'high_watermark':100000}}


def check_universe(rec,strategy):
    assert rec['strategy']==strategy,rec['strategy']
    assert {row['instrument_id'] for row in rec['target_weights']}<=set(EIGHT),rec['target_weights']
    assert abs(sum(row['weight'] for row in rec['target_weights'])+rec['cash_weight']-1)<1e-7
    assert rec['snapshot_checksum'].startswith('sha256:')


def test_frozen_classical_recommendation_explanation_and_retrieval(scenario):
    plan=call(MV,scenario)
    rec=plan['recommendation']
    check_universe(rec,'min_variance')
    assert rec['input_snapshot_id']==scenario['input_snapshot_id'] and rec['as_of']==scenario['as_of']
    assert all({'current_quantity','target_quantity','delta_quantity','reference_price','price_as_of'} <= set(row) for row in rec['decisions'])
    fetched=call('get_classical_analysis',{'analysis_id':plan['analysis_id']})
    assert fetched==plan
    why=call('explain_classical_recommendation',{'analysis_id':plan['analysis_id'],'instrument_id':'GOOGL'})
    assert why['analysis_kind']=='explanation'
    assert why['source_analysis_id']==plan['analysis_id']
    assert why['source_analysis_ref']==plan['analysis_ref']


def test_cvar_recommendation_targets_only_the_requested_universe(scenario):
    plan=call(CVAR,{**scenario,'settings':{'cvar_alpha':0.95}})
    check_universe(plan['recommendation'],'cvar')


def test_ppo_recommendation_uses_the_policy_of_exactly_the_requested_universe(scenario):
    doc=call(PPO,scenario)
    check_universe(doc['recommendation'],'ppo')


@pytest.mark.parametrize('tool,instruments',[(MV,['AAPL','ZZZZQ']),(CVAR,['AAPL','ZZZZQ']),(PPO,['AAPL','VOO'])])
def test_unservable_universe_is_a_typed_refusal(scenario,tool,instruments):
    request={**scenario,'instruments':instruments,'holdings':{**scenario['holdings'],'weights':[{'instrument_id':i,'weight':0} for i in instruments]}}
    doc=invoke(tool,request)
    validate_result(tool,doc)
    assert is_error(doc) and doc['code'] in UNIVERSE_REFUSALS,doc


@pytest.mark.parametrize('tool',[MV,CVAR,PPO])
def test_instruments_are_required(scenario,tool):
    doc=invoke(tool,{k:v for k,v in scenario.items() if k!='instruments'})
    validate_result(tool,doc)
    assert is_error(doc) and doc['code']=='VALIDATION_FAILED',doc


def test_ci_paid_research_is_denied_before_model_call():
    doc=invoke('run_portfolio_research',{'review_id':'ca_'+'1'*32,'dry_run':False,'confirmed_by_user':True,'idempotency_key':'ci-paid-denial'})
    validate_result('run_portfolio_research',doc)
    assert is_error(doc) and doc['code']=='FORBIDDEN',doc


@pytest.mark.parametrize('tool',[MV,CVAR])
def test_classical_input_is_strict_and_does_not_accept_an_algorithm(tool):
    doc=invoke(tool,{'instruments':EIGHT,'algorithm':'ppo'})
    validate_result(tool,doc)
    assert is_error(doc) and doc['code']=='VALIDATION_FAILED',doc


def test_financial_news_context_keeps_validated_public_citations():
    from finplan_tools.tools.classical import _validate_public_citation
    doc=call('get_etf_research_inputs',{'symbols':['VOO','GOOGL']})
    assert doc['symbols']==['VOO','GOOGL']
    assert {'features','models','provenance','limitations','decision_at'} <= set(doc)
    # News availability may expire or collection may have stopped. Schema and
    # citation safety remain testable without promising permanently fresh news.
    for row in doc['models'].get('llm',{}).get('context',[]):
        _validate_public_citation(row['url'])
        assert isinstance(row.get('title'),str)
    assert doc['models']['ppo']['eligible'] is False
    assert doc['models']['llm']['trading_policy_eligible'] is False
