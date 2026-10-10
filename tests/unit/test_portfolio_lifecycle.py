"""MCP lifecycle wiring, verified human mutation and durable successful/failed receipts."""
from __future__ import annotations

import copy
import base64
import hashlib
import json
from types import SimpleNamespace

import pytest
from finplan_contracts.schemas import contracts_root
from finplan_tools.core.activity import sanitize
from finplan_tools.core.contracts import validate_document
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
            if name=='record_agent_activity':
                # The real Platform rejects mismatched body and authenticated header IDs.
                assert args[0]['correlation_id']==args[1].correlation_id
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


def test_activity_history_defaults_to_authoritative_saved_book(offline):
    platform=setup(offline)
    doc=invoke('list_agent_activity',offline.event({}),None,offline.runtime)
    assert 'events' in doc,doc
    assert platform.calls[0][0]=='get_plan'
    assert platform.calls[1][2]=={'page_size':3,'portfolio_id':PF}


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
    assert platform.calls[0][1][1].correlation_id==args['correlation_id']
    assert 'amazonaws' not in json.dumps(body)
    # Explicit evidence archive does not recursively archive itself.
    assert len(platform.calls)==1


def restore_archived_diagnostic(value):
    if isinstance(value, dict):
        if value.get('representation') == 'json_pointer':
            assert value['encoding'] == 'base64url_utf8_segments'
            parts = [base64.urlsafe_b64decode(s + '=' * (-len(s) % 4)).decode() for s in value['segments']]
            return '/' + '/'.join(parts) if parts else ''
        if value.get('representation') == 'json_pointer_diagnostic':
            return restore_archived_diagnostic(value['pointer']) + value['suffix']
        return {key: restore_archived_diagnostic(item) for key, item in value.items()}
    if isinstance(value, list):
        return [restore_archived_diagnostic(item) for item in value]
    return value


@pytest.mark.parametrize('pointer', ['', '/', '/explanation/type', '//items/0/', '/a~1b/~0/café/💹', '/arn:aws:s3:::bucket/s3:~1~1private'])
def test_archived_pointer_segments_roundtrip_without_path_like_contract_values(offline, pointer):
    platform = setup(offline)
    original = {'pointer': pointer, 'message': (pointer or '/') + ': invalid value', 'schema_path': '/properties/items'}
    args = {'event_kind': 'agent_turn', 'correlation_id': 'corr-pointer-roundtrip', 'idempotency_key': 'pointer-roundtrip', 'payload': original}
    result = invoke('record_agent_activity', offline.event(args), None, offline.runtime)
    assert result.get('activity_event_id'), result
    saved = platform.calls[0][1][0]['payload']
    assert saved['pointer']['representation'] == 'json_pointer'
    assert saved['message']['representation'] == 'json_pointer_diagnostic'
    assert restore_archived_diagnostic(saved) == original
    assert sanitize(saved) == saved  # Agent and adapter sanitize the same evidence independently.
    assert args['payload'] == original


@pytest.mark.parametrize('arguments', [{'decision_id': 'invalid'}, {}, {'decision_id': PD, 'unexpected': 1}])
def test_failed_request_archives_the_exact_source_error_and_keeps_public_json_pointers(offline, arguments):
    platform = setup(offline)
    offline.runtime.durable_activity_receipts = True
    receipts = []

    def archive(body, meta):
        validation = validate_document(body, 'tools/record-agent-activity-request')
        assert validation.valid, validation.to_dict()
        assert body['correlation_id'] == meta.correlation_id
        receipts.append(copy.deepcopy(body))
        return fixture('record_agent_activity')

    platform.record_agent_activity = archive
    result = invoke('get_portfolio_decision', offline.event(arguments), None, offline.runtime)
    assert result['code'] in ('INVALID_IDENTIFIER', 'VALIDATION_FAILED'), result
    assert isinstance(result['details']['pointer'], str)
    assert len(receipts) == 1
    assert receipts[0].get('decision_id') in (None, PD)
    assert receipts[0]['payload']['status'] == result['code']
    assert restore_archived_diagnostic(receipts[0]['payload']['response']) == result


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


def test_repeated_activity_reads_archive_references_without_recursive_payload_growth(offline):
    platform=setup(offline)
    offline.runtime.durable_activity_receipts=True
    base=fixture('list_agent_activity')['events'][0]
    base['payload']={'narrative':'original financial evidence '+('x'*20000)}
    base['checksum']='sha256:'+hashlib.sha256(json.dumps(base,sort_keys=True).encode()).hexdigest()
    immutable_original=copy.deepcopy(base)
    events=[base]
    receipt_sizes=[]
    pages=[]

    def read(meta,**query):
        page={'events':copy.deepcopy(list(reversed(events[-3:]))),'next_token':None,'contract_version':'1.5.0'}
        pages.append(copy.deepcopy(page))
        return page

    def archive(body,meta):
        assert body['correlation_id']==meta.correlation_id
        receipt_sizes.append(len(json.dumps(body)))
        event={k:copy.deepcopy(v) for k,v in body.items() if k!='idempotency_key'}
        event.update(activity_event_id='act_'+str(len(events)).zfill(26),recorded_at='2026-01-09T15:00:00Z',contract_version='1.5.0',caller={'principal':'synthetic'})
        event['checksum']='sha256:'+hashlib.sha256(json.dumps(event,sort_keys=True).encode()).hexdigest()
        events.append(event)
        return {'activity_event_id':event['activity_event_id']}

    platform.list_agent_activity=read
    platform.record_agent_activity=archive
    for _ in range(40):
        response=invoke('list_agent_activity',offline.event({'portfolio_id':PF,'limit':3}),None,offline.runtime)
        assert response==pages[-1],response  # Public MCP payloads remain exact, full records.
        retained=events[-1]['payload']['response']
        assert retained['next_token']==response['next_token']
        assert retained['evidence_representation']=='immutable_activity_references'
        assert [(r['activity_event_id'],r['checksum']) for r in retained['events']]==[(r['activity_event_id'],r['checksum']) for r in response['events']]
        assert all('payload' not in row for row in retained['events'])
        assert receipt_sizes[-1]<7000
    assert events[0]==immutable_original
    assert max(receipt_sizes[-20:])-min(receipt_sizes[-20:])<100
    assert len(json.dumps(pages[-1]))<24000


@pytest.mark.parametrize('tool,key,id_key,payload_key',[
    ('list_agent_activity','events','activity_event_id','payload'),
    ('list_portfolio_decisions','decisions','decision_id','provenance'),
    ('get_portfolio_history','history','decision_id','resolution'),
    ('list_market_snapshots','snapshots','input_snapshot_id','quality_details'),
])
def test_large_history_pages_follow_native_cursors_without_loss_after_new_inserts(offline,tool,key,id_key,payload_key):
    from finplan_tools.core.bounds import response_size
    platform=setup(offline)
    template=fixture(tool)
    if key=='snapshots':
        examples=(json.loads(p.read_text()) for p in (contracts_root()/'fixtures/input-snapshot/valid').glob('*.json'))
        template[key]=[next(doc for doc in examples if doc['status']=='approved')]
    prefix={'events':'act_','decisions':'pd_','history':'pd_','snapshots':'snap_'}[key]
    original=[]
    for i in range(8):
        row=copy.deepcopy(template[key][0])
        row[id_key]=prefix+str(i).zfill(26)
        row.setdefault(payload_key,{})['evidence']='x'*24000
        original.append(row)
    records=copy.deepcopy(original)
    producer_calls=[]

    def native_cursor(row):
        return base64.urlsafe_b64encode(json.dumps({'portfolio_id':PF,id_key:row[id_key]}).encode()).decode().rstrip('=')

    def read(*args,**query):
        producer_calls.append(copy.deepcopy(query))
        token=query.get('next_token')
        cursor=json.loads(base64.urlsafe_b64decode(token+'='*(-len(token)%4))) if token else None
        if cursor:
            assert cursor['portfolio_id']==PF and 't' not in cursor
        eligible=sorted((r for r in records if not cursor or r[id_key]<cursor[id_key]),key=lambda r:r[id_key],reverse=True)
        page=eligible[:query['page_size']]
        return {**template,key:copy.deepcopy(page),'next_token':native_cursor(page[-1]) if len(eligible)>len(page) else None}

    setattr(platform,tool,read)
    request={'limit':8,**({'portfolio_id':PF} if key!='snapshots' else {})}
    received=[]
    for iteration in range(8):
        response=invoke(tool,offline.event(request),None,offline.runtime)
        assert key in response,response
        assert response_size(response)<=65536
        received.extend(response[key])
        token=response['next_token']
        if not token:
            break
        # Newer records must not shift the continuation offset or be replayed.
        newer=copy.deepcopy(original[0])
        newer[id_key]=prefix+str(100+iteration).zfill(26)
        records.append(newer)
        request['next_token']=token
    else:
        pytest.fail('history pagination did not terminate')
    assert received==list(reversed(original))
    assert len({row[id_key] for row in received})==len(original)
    assert any(query['page_size']==1 for query in producer_calls)


@pytest.mark.parametrize('tool,key',[
    ('get_portfolio_history','history'),('list_portfolio_decisions','decisions'),
    ('list_market_snapshots','snapshots'),('list_agent_activity','events'),
])
def test_all_lifecycle_lists_return_native_producer_cursors_without_wrapping(offline,tool,key):
    platform=setup(offline)
    token=base64.urlsafe_b64encode(json.dumps({'portfolio_id':PF,'record_id':'immutable-record'}).encode()).decode().rstrip('=')
    doc=fixture(tool)
    doc['next_token']=token
    calls=[]
    def read(*args,**query):
        calls.append(query)
        return copy.deepcopy(doc)
    setattr(platform,tool,read)
    request={'next_token':token}
    if tool!='list_market_snapshots':
        request['portfolio_id']=PF
    result=invoke(tool,offline.event(request),None,offline.runtime)
    assert result==doc
    assert calls[0]['next_token']==token


def test_lifecycle_history_rejects_obsolete_tool_offset_token_before_producer(offline):
    from finplan_tools.core.bounds import wrap_token
    platform=setup(offline)
    token=wrap_token(tool='list_agent_activity',environment='beta',offset=2)
    result=invoke('list_agent_activity',offline.event({'portfolio_id':PF,'next_token':token}),None,offline.runtime)
    assert result['code']=='VALIDATION_FAILED' and not platform.calls


def test_oversized_activity_payload_reassembles_exactly_and_resumes_older_records(offline):
    from finplan_tools.core.bounds import response_size
    platform=setup(offline)
    offline.runtime.durable_activity_receipts=True
    template=fixture('list_agent_activity')
    original=copy.deepcopy(template['events'][0])
    original['payload']={'narrative':'Market evidence € 🧪 "quoted"\n'*8000,'tools':[{'result':{'weights':[.2,.3,.5]}}]}
    older=copy.deepcopy(template['events'][0])
    older['activity_event_id']='act_'+('0'*25)+'1'
    original_id=original['activity_event_id']
    native=base64.urlsafe_b64encode(json.dumps({'portfolio_id':PF,'activity_event_id':original_id}).encode()).decode().rstrip('=')
    calls=[]
    def read(meta,**query):
        calls.append(copy.deepcopy(query))
        token=query.get('next_token')
        if token and token.startswith('ae1_'):
            state=json.loads(base64.urlsafe_b64decode(token[4:]+'='*(-len(token[4:])%4)))
            assert state=={'activity_event_id':original_id} and query['portfolio_id']==PF
            return {**template,'events':[copy.deepcopy(original)],'next_token':None}
        if token:
            assert token==native
            return {**template,'events':[copy.deepcopy(older)],'next_token':None}
        return {**template,'events':[copy.deepcopy(original)]+([copy.deepcopy(older)] if query['page_size']>1 else []),'next_token':native if query['page_size']==1 else None}
    platform.list_agent_activity=read
    request={'portfolio_id':PF,'limit':3}
    fragments=[]
    offset=0
    first_token=None
    for _ in range(30):
        result=invoke('list_agent_activity',offline.event(request),None,offline.runtime)
        assert 'events' in result,result
        assert response_size(result)<=65536
        event=result['events'][0]
        if event['activity_event_id']==older['activity_event_id']:
            assert event==older and result['next_token'] is None
            break
        assert event['activity_event_id']==original_id and event['checksum']==original['checksum']
        payload=event['payload']
        assert payload['representation']=='chunked_immutable_json'
        assert payload['offset']==offset
        offset=payload['end_offset']
        fragments.append(payload['fragment'])
        receipt=platform.calls[-1][1][0]['payload']['response']['events'][0]
        assert 'payload' not in receipt and 'fragment' not in receipt['summary']
        assert receipt['summary']['payload_checksum']==payload['payload_checksum']
        assert receipt['summary']['offset']==payload['offset']
        request['next_token']=result['next_token']
        first_token=first_token or result['next_token']
    else:
        pytest.fail('activity payload chunks did not terminate')
    raw=''.join(fragments).encode('utf-8')
    assert json.loads(raw)==original['payload']
    assert offset==payload['total']==len(raw)
    assert payload['payload_checksum']=='sha256:'+hashlib.sha256(raw).hexdigest()
    assert len(fragments)>2 and any(c.get('next_token','').startswith('ae1_') for c in calls)
    # A valid chunk cursor cannot be reused for another portfolio or session.
    count=len(calls)
    wrong=invoke('list_agent_activity',offline.event({'session_id':'other-session','next_token':first_token}),None,offline.runtime)
    assert wrong['code']=='VALIDATION_FAILED' and len(calls)==count
    # Even if an unexpected storage mutation retains the old object checksum,
    # the independently verified canonical payload checksum detects the change.
    original['payload']['narrative']='changed'
    mismatch=invoke('list_agent_activity',offline.event({'portfolio_id':PF,'next_token':first_token}),None,offline.runtime)
    assert mismatch['code']=='CONFLICT'
