"""Real pipeline, same-environment backend and bounded research preflight regressions."""
from __future__ import annotations

import copy
import io
import json
from types import SimpleNamespace

import pytest
from finplan_contracts import boundaries
from finplan_contracts.iam import Request, evaluate
from finplan_contracts.schemas import contracts_root
from finplan_tools.backends.classical import ClassicalLambdaClient
from finplan_tools.backends.jobs import JobClient
from finplan_tools.core.errors import ToolError
from finplan_tools.core.transport import CallMeta
from finplan_tools.tools.classical import _authorize_research
from infra.stacks.policies import role_class_policy

ACCOUNT='1'*12
ARN=f'arn:aws:lambda:us-east-2:{ACCOUNT}:function:finplan-beta-financemodel-job-api-handler-classical'
AID='ca_'+'1'*32


def evidence(kind='recommendation'):
    tool = {'recommendation':'recommend-classical-portfolio','explanation':'explain-classical-recommendation','comparison':'compare-classical-plans','performance':'evaluate-classical-performance','research':'research-portfolio-models','market_events':'research-market-events','research_run':'run-portfolio-research','feedback':'submit-portfolio-feedback'}[kind]
    path=contracts_root()/f'fixtures/tools/{tool}-response/valid/synthetic.json'
    return json.loads(path.read_text())


class Client:
    def __init__(self, document=None):
        self.document=document if document is not None else evidence()
        self.calls=[]
    def invoke(self, **kwargs):
        self.calls.append(kwargs)
        return {'StatusCode':200,'Payload':io.BytesIO(json.dumps(self.document,allow_nan=False).encode())}


def backend(client, arn=ARN):
    return ClassicalLambdaClient(client,SimpleNamespace(get=lambda *args:arn),environment='beta',region='us-east-2',account=ACCOUNT)


def wire(offline, client):
    offline.runtime.jobs=lambda timeout:JobClient(offline.jobs,timeout=timeout,classical_client=backend(client))


def test_classical_call_preserves_math_evidence_and_passes_identity_headers(offline,invoke):
    client=Client()
    wire(offline,client)
    request={'algorithm':'min_variance','settings':{'lookback_days':60}}
    assert invoke(offline,'recommend_classical_portfolio',request)==client.document
    payload=json.loads(client.calls[0]['Payload'])
    assert payload['operation']=='recommend_classical_portfolio'
    assert payload['environment']=='beta' and payload['request']==request
    assert payload['headers']['X-Finplan-Contract-Version']=='1.4.0'
    assert json.loads(payload['headers']['X-Finplan-Caller'])['channel']=='direct_test'
    assert offline.jobs.count()==0 and offline.platform.count()==0


@pytest.mark.parametrize('arn',[None, ARN.replace('beta','gamma'),ARN.replace(ACCOUNT,'9'*12), ARN+':other'])
def test_bad_reference_cannot_invoke(arn):
    client=Client()
    with pytest.raises(ToolError):
        backend(client,arn).call('recommend_classical_portfolio',{},CallMeta('corr-test-0001','1.4.0'))
    assert not client.calls


def test_unknown_operation_cannot_invoke():
    client=Client()
    with pytest.raises(ToolError):
        backend(client).call('recommend_portfolio',{},CallMeta('corr-test-0001','1.4.0'))
    assert not client.calls


@pytest.mark.parametrize('document',[
    {'algorithm':'ppo'},
    {'settings':{'lookback_days':900}},
    {'output_uri':'s3:'+'//no-bucket/path'},
    {'as_of':'2026-01-09'},
])
def test_invalid_input_refused_before_producer(offline,invoke,document):
    client=Client();wire(offline,client)
    response=invoke(offline,'recommend_classical_portfolio',document)
    assert response['code']=='VALIDATION_FAILED'
    assert not client.calls


def test_wrong_identifier_has_registered_error(offline,invoke):
    client=Client();wire(offline,client)
    response=invoke(offline,'get_classical_analysis',{'analysis_id':'run_01JA2B3C4D5E6F7G8H9JKMNPQR'})
    assert response['code']=='INVALID_IDENTIFIER' and response['details']['field']=='analysis_id'
    assert not client.calls


def test_producer_errors_keep_retryability(offline,invoke):
    client=Client({'code':'NOT_FOUND','message':'No analysis','retryable':False,'details':{},'correlation_id':'corr-test-0001','contract_version':'1.4.0'})
    wire(offline,client)
    response=invoke(offline,'get_classical_analysis',{'analysis_id':AID})
    assert response['code']=='NOT_FOUND' and response['retryable'] is False


def test_reference_mismatch_is_not_published(offline,invoke):
    doc=evidence();doc['analysis_ref']['artifact_id']='ca_'+'2'*32
    client=Client(doc);wire(offline,client)
    response=invoke(offline,'recommend_classical_portfolio',{})
    assert response['code']=='INTERNAL'


def test_old_producer_is_not_called(offline,invoke):
    manifest=offline.runtime.manifest('financemodel');manifest['contract_version']='1.3.0'
    offline.set_param('financemodel','release','manifest',manifest)
    client=Client();wire(offline,client)
    response=invoke(offline,'recommend_classical_portfolio',{})
    assert response['code']=='DEPENDENCY_UNAVAILABLE'
    assert not client.calls


def test_ci_paid_run_rejected_before_producer(offline,invoke):
    client=Client(evidence('research_run'));wire(offline,client)
    response=invoke(offline,'run_portfolio_research',{'review_id':AID,'dry_run':False,'confirmed_by_user':True,'idempotency_key':'weekly-test'},source='ci_test')
    assert response['code']=='FORBIDDEN' and not client.calls


def test_reader_and_gateway_ci_cannot_start_paid_research():
    for groups in [frozenset(),frozenset({'viewer'}),frozenset({'ci_test'}),frozenset({'plan_publisher'})]:
        with pytest.raises(ToolError) as err:
            _authorize_research(SimpleNamespace(source='gateway',groups=groups),{'dry_run':False,'confirmed_by_user':True},{})
        assert err.value.code=='FORBIDDEN'
    _authorize_research(SimpleNamespace(source='gateway',groups={'researcher'}),{'dry_run':False,'confirmed_by_user':True},{})


def test_paid_research_estimate_over_cap_never_submits(offline,invoke):
    client=Client({**evidence('research_run'),'cost_estimate':{**evidence('research_run')['cost_estimate'],'estimated_usd_upper_bound':.51}})
    wire(offline,client)
    response=invoke(offline,'run_portfolio_research',{'review_id':AID,'dry_run':False,'confirmed_by_user':True,'idempotency_key':'weekly-test'})
    assert response['code']=='BUDGET_EXCEEDED'
    assert len(client.calls)==1 and json.loads(client.calls[0]['Payload'])['request']['dry_run'] is True


def test_paid_research_derives_key_and_preflights_once(offline,invoke):
    client=Client({**evidence('research_run'),'cost_estimate':{**evidence('research_run')['cost_estimate'],'estimated_usd_upper_bound':.25}})
    wire(offline,client)
    response=invoke(offline,'run_portfolio_research',{'review_id':AID,'dry_run':False,'confirmed_by_user':True,'idempotency_key':'weekly-test'})
    assert 'code' not in response,response
    payloads=[json.loads(c['Payload'])['request'] for c in client.calls]
    assert len(payloads)==2 and payloads[0]['dry_run'] is True and payloads[1]['dry_run'] is False
    assert payloads[1]['idempotency_key'].startswith('lt_')
    assert payloads[1]['confirmed_by_user'] is True
    assert payloads[0]['idempotency_key']==payloads[1]['idempotency_key']


def test_feedback_is_idempotent_audit_without_confirmation(offline,invoke):
    client=Client(evidence('feedback'));wire(offline,client)
    response=invoke(offline,'submit_portfolio_feedback',{'analysis_id':AID,'text':'Explain volatility assumption','idempotency_key':'feedback-1'},source='ci_test')
    assert 'code' not in response,response
    payload=json.loads(client.calls[0]['Payload'])['request']
    assert payload['idempotency_key'].startswith('lt_') and 'confirmed_by_user' not in payload


def test_public_news_links_survive_but_storage_links_do_not(offline,invoke):
    doc={**evidence('market_events'),'sources':[{'url':'https://example.org/news','title':'Public dated report'}]}
    client=Client(doc);wire(offline,client)
    assert invoke(offline,'research_market_events',{'analysis_id':AID})==doc
    bad=copy.deepcopy(doc);bad['sources'][0]['url']='https://'+'private'+'.s3.amazonaws.com/data'
    client.document=bad
    assert invoke(offline,'research_market_events',{'analysis_id':AID})['code']=='INTERNAL'


@pytest.mark.parametrize('role',['reader','submitter'])
def test_only_own_classical_function_can_be_invoked(role):
    kw={'partition':'aws','region':'us-east-2','account':ACCOUNT}
    policy=role_class_policy('beta',role,**kw)
    boundary=boundaries.env_permission_boundary('beta',**kw)
    def allowed(resource):return evaluate(Request('lambda:InvokeFunction',resource),{'identity':policy},boundary).allowed
    assert allowed(ARN) and allowed(ARN+':$LATEST')
    for other in [ARN.replace('beta','gamma'),ARN.replace('classical','dispatcher'),ARN+':1']:
        assert not allowed(other)
