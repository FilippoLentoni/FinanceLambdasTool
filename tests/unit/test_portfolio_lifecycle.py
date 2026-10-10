"""MCP lifecycle wiring, verified human mutation and durable successful/failed receipts."""
from __future__ import annotations

import copy
import json
from types import SimpleNamespace

import pytest
from finplan_contracts.schemas import contracts_root
from finplan_tools.core.errors import ToolError
from finplan_tools.handler import invoke
from infra.stacks.policies import ProducerApis, role_class_policy
from finplan_contracts import boundaries
from finplan_contracts.iam import Request, evaluate

PF='pf_01JA2B3C4D5E6F7G8H9JKMNPQR'
PD='pd_01JA2B3C4D5E6F7G8H9JKMNPQR'


def fixture(tool):
    return json.loads((contracts_root()/f'fixtures/tools/{tool.replace("_","-")}-response/valid/synthetic-lifecycle.json').read_text())


class Platform:
    def __init__(self):
        self.calls=[]
        self.error=None
    def get_plan(self,plan_id,meta):
        self.calls.append(('get_plan',plan_id,meta))
        return {'plan':{'portfolio_id':PF}}
    def __getattr__(self,name):
        def call(*args,**kwargs):
            self.calls.append((name,args,kwargs))
            if self.error and name!='record_agent_activity':
                raise self.error
            return copy.deepcopy(fixture(name))
        return call


def setup(offline):
    platform=Platform()
    offline.runtime.platform=lambda _:platform
    offline.runtime.references=SimpleNamespace(get=lambda *args:'pl_01JA2B3C4D5E6F7G8H9JKMNPQR', tool_limits_document=lambda:None)
    offline.runtime.manifest=lambda producer:{'contract_version':'1.5.0','served_contract_majors':[1]}
    return platform


def context(tool,token='valid-human',gateway='paper'):
    return SimpleNamespace(client_context=SimpleNamespace(custom={'bedrockAgentCoreToolName':gateway+'___'+tool,'bedrockAgentCorePropagatedHeaders':{'X-Finplan-User-Token':token}}))


@pytest.mark.parametrize('tool,key',[('get_portfolio_history','history'),('list_portfolio_decisions','decisions')])
def test_default_saved_portfolio_and_three_records(offline,tool,key):
    platform=setup(offline)
    doc=invoke(tool,offline.event({}),None,offline.runtime)
    assert key in doc,doc
    assert platform.calls[0][0]=='get_plan'
    _,args,query=platform.calls[1]
    assert args[0]==PF and query=={'page_size':3}


def test_snapshot_discovery_never_downloads_market_provider(offline):
    platform=setup(offline)
    doc=invoke('list_market_snapshots',offline.event({}),None,offline.runtime)
    assert 'snapshots' in doc,doc
    assert platform.calls[0][2]=={'dataset_id':'finance/equity-etf-daily/research-universe','page_size':3}


def test_verified_human_resolution_forwards_hash_and_derived_idempotency(offline):
    platform=setup(offline)
    offline.runtime.gateway_research_authorizer=SimpleNamespace(verify_context=lambda c:SimpleNamespace(identity='cognito:verified-user-hash',groups=frozenset({'viewer'})))
    args={'decision_id':PD,'action':'accept','expected_revision':1,'confirmed_by_user':True,'idempotency_key':'human-approved-plan'}
    doc=invoke('resolve_portfolio_decision',args,context('resolve_portfolio_decision'),offline.runtime)
    assert doc['status']=='accepted',doc
    body=platform.calls[0][1][1]
    meta=platform.calls[0][1][2]
    assert body['idempotency_key'].startswith('lt_') and body['idempotency_key']!=args['idempotency_key']
    assert body['confirmed_by_user'] is True and 'decision_id' not in body
    assert meta.caller['subject_hash'].startswith('sha256:')
    assert 'valid-human' not in json.dumps(body)


@pytest.mark.parametrize('source',['direct_test','ci_test'])
def test_machine_marker_or_spoofed_group_cannot_accept(offline,source):
    platform=setup(offline)
    args={'decision_id':PD,'action':'accept','expected_revision':1,'confirmed_by_user':True,'idempotency_key':'spoofed'}
    doc=invoke('resolve_portfolio_decision',offline.event(args,source=source),None,offline.runtime)
    assert doc['code']=='FORBIDDEN' and not platform.calls


def test_missing_verified_token_never_reaches_resolution(offline):
    platform=setup(offline)
    args={'decision_id':PD,'action':'reject','expected_revision':1,'confirmed_by_user':True,'idempotency_key':'reject'}
    doc=invoke('resolve_portfolio_decision',args,context('resolve_portfolio_decision',None),offline.runtime)
    assert doc['code']=='FORBIDDEN' and not platform.calls


@pytest.mark.parametrize('failure',[False,True])
def test_successful_and_failed_receipts_keep_inputs_results_but_no_tokens(offline,failure):
    platform=setup(offline)
    offline.runtime.durable_activity_receipts=True
    if failure:
        platform.error=ToolError('NOT_FOUND','decision missing')
    doc=invoke('get_portfolio_decision',offline.event({'decision_id':PD}),None,offline.runtime)
    assert ('code' in doc)==failure
    assert platform.calls[-1][0]=='record_agent_activity'
    receipt=platform.calls[-1][1][0]
    assert receipt['decision_id']==PD
    assert receipt['payload']['request']=={'decision_id':PD}
    assert receipt['payload']['response']==doc
    assert receipt['payload']['status']==('NOT_FOUND' if failure else 'OK')


def test_activity_archive_redacts_nested_credentials_private_locations_and_preserves_citations(offline):
    platform=setup(offline)
    args={'event_kind':'agent_turn','correlation_id':'corr-archive-test','idempotency_key':'archive-test',
          'payload':{'Authorization':'Bearer secret','nested':{'refresh_token':'secret','download_grant':'https://'+'bucket.s3.'+'amazonaws.com/key?X-Amz-Signature=secret'},'sources':[{'url':'https://arxiv.org/abs/1234.56789'}]}}
    doc=invoke('record_agent_activity',offline.event(args),None,offline.runtime)
    assert doc.get('activity_event_id'),doc
    body=platform.calls[0][1][0]
    assert body['payload']['Authorization']=='<redacted>'
    assert body['payload']['nested']['refresh_token']=='<redacted>'
    assert body['payload']['sources']==args['payload']['sources']
    assert 'amazonaws' not in json.dumps(body)
    # Explicit evidence archive does not recursively archive itself.
    assert len(platform.calls)==1


def test_reader_only_post_exception_is_durable_evidence_endpoint():
    kw={'partition':'aws','region':'us-east-2','account':'1'*12}
    apis=ProducerApis(plan_id='planapi000',plan_stage='live')
    policy=role_class_policy('beta','reader',apis=apis,**kw)
    boundary=boundaries.env_permission_boundary('beta',**kw)
    prefix='arn:aws:execute-api:us-east-2:'+kw['account']+':planapi000/live/'
    for method,path,allowed in [('GET','v1/portfolio-decisions/'+PD,True),('POST','v1/activity-events',True),('POST','v1/portfolio-decisions/'+PD+'/resolution',False),('POST','v1/plans/x/versions',False)]:
        assert evaluate(Request('execute-api:Invoke',prefix+method+'/'+path),{'identity':policy},boundary).allowed is allowed


@pytest.mark.parametrize('gateway',['primary','classical'])
def test_both_gateway_names_require_explicit_confirmation_and_preserve_replay_key(offline,gateway):
    platform=setup(offline)
    offline.runtime.gateway_research_authorizer=SimpleNamespace(verify_context=lambda c:SimpleNamespace(identity='cognito:verified-user-hash',groups=frozenset({'viewer'})))
    args={'decision_id':PD,'action':'accept','expected_revision':1,'confirmed_by_user':False,'idempotency_key':'same-user-decision'}
    denied=invoke('resolve_portfolio_decision',args,context('resolve_portfolio_decision',gateway=gateway),offline.runtime)
    assert denied['code'] in ('PRECONDITION_FAILED','VALIDATION_FAILED') and not platform.calls
    args['confirmed_by_user']=True
    first=invoke('resolve_portfolio_decision',args,context('resolve_portfolio_decision',gateway=gateway),offline.runtime)
    second=invoke('resolve_portfolio_decision',args,context('resolve_portfolio_decision',gateway=gateway),offline.runtime)
    assert first==second and first['status']=='accepted'
    assert platform.calls[0][1][1]['idempotency_key']==platform.calls[1][1][1]['idempotency_key']


def test_mandatory_boundary_preserves_cross_environment_denial_after_inline_deduplication():
    kw={'partition':'aws','region':'us-east-2','account':'1'*12}
    for env in ('beta','gamma','prod'):
        boundary=boundaries.env_permission_boundary(env,**kw)
        # Even an accidentally broad future allow cannot overcome the boundary.
        broad={'Version':'2012-10-17','Statement':[{'Effect':'Allow','Action':'*','Resource':'*'}]}
        for other in set(('beta','gamma','prod'))-{env}:
            resources=[('ssm:GetParameter','arn:aws:ssm:us-east-2:'+kw['account']+':parameter/finplan/'+other+'/financialplanning/config/x'),
                       ('lambda:InvokeFunction','arn:aws:lambda:us-east-2:'+kw['account']+':function:finplan-'+other+'-financelambdastool-get-plan'),
                       ('s3:GetObject','arn:aws:s3:::finplan-'+other+'-financialplanning-snapshots-'+kw['account']+'/x')]
            for action,resource in resources:
                assert not evaluate(Request(action,resource),{'identity':broad},boundary).allowed
