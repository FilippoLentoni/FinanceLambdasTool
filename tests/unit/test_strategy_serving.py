import io
import json
from types import SimpleNamespace

import pytest
from finplan_contracts import boundaries
from finplan_contracts.iam import Request, evaluate

from finplan_tools.backends.strategy import StrategyLambdaClient
from finplan_tools.core.config import ToolLimits
from finplan_tools.core.errors import ToolError
from finplan_tools.core.transport import CallMeta
from infra.stacks.policies import role_class_policy

ACCOUNT = '1' * 12
ARN = f'arn:aws:lambda:us-east-2:{ACCOUNT}:function:finplan-beta-financemodel-job-api-handler-inference'


class Client:
    def __init__(self, body=b'{"recommendation": {}}', *, failure=None):
        self.body, self.failure, self.calls = body, failure, []

    def invoke(self, **kwargs):
        self.calls.append(kwargs)
        if self.failure:
            raise self.failure
        return {'StatusCode':200, 'Payload':io.BytesIO(self.body)}


def backend(client, arn=ARN):
    refs = SimpleNamespace(get=lambda *args: arn)
    return StrategyLambdaClient(client, refs, environment='beta', region='us-east-2', account=ACCOUNT)


def test_direct_invocation_preserves_environment_identity_and_correlation():
    client = Client()
    meta = CallMeta('corr-test-0001', '1.2.0', {'actor':'test-user'})
    assert backend(client).recommend({'as_of':'2026-10-08'}, meta) == {'recommendation':{}}
    assert len(client.calls) == 1
    call = client.calls[0]
    assert call['FunctionName'] == ARN and call['InvocationType'] == 'RequestResponse'
    payload = json.loads(call['Payload'])
    assert payload['environment'] == 'beta' and payload['headers'] == meta.headers()


@pytest.mark.parametrize('arn', [None, ARN.replace('beta','prod'), ARN.replace(ACCOUNT,'9' * 12), ARN+':other', 'https://endpoint.invalid'])
def test_bad_reference_never_invokes_a_function(arn):
    client = Client()
    with pytest.raises(ToolError):
        backend(client, arn).recommend({}, CallMeta('corr-test-0001','1.2.0'))
    assert not client.calls


@pytest.mark.parametrize('body', [b'not-json', b'[]', b'{"value": NaN}', b'x'*65537])
def test_corrupt_or_oversized_response_fails_without_retry(body):
    client = Client(body)
    with pytest.raises(ToolError):
        backend(client).recommend({}, CallMeta('corr-test-0001','1.2.0'))
    assert len(client.calls) == 1


def test_access_denial_is_not_retried():
    class Denied(Exception):
        response = {'Error':{'Code':'AccessDeniedException'}}
    client = Client(failure=Denied())
    with pytest.raises(ToolError) as err:
        backend(client).recommend({}, CallMeta('corr-test-0001','1.2.0'))
    assert err.value.code == 'FORBIDDEN' and len(client.calls) == 1


def test_legacy_timeout_configuration_receives_new_tool_deadline():
    limits = ToolLimits.from_document({'timeouts_seconds':{'read':17,'write':30,'refresh_market_data':60}})
    assert limits.timeouts_seconds['recommendation'] == 300
    assert limits.timeouts_seconds['read'] == 17


def test_runtime_sdk_deadline_and_retry_configuration(monkeypatch):
    from finplan_tools.core import aws_clients
    from finplan_tools.core.pipeline import Runtime
    calls = []
    class Session:
        def client(self, name, **kwargs):
            calls.append((name, kwargs))
            return SimpleNamespace()
    monkeypatch.setattr(aws_clients, 'session', lambda region: Session())
    monkeypatch.setattr(aws_clients, 'ssm_client', lambda *a, **kw: SimpleNamespace())
    Runtime.from_environment({'FINPLAN_ENV':'beta','FINPLAN_ACCOUNT_ID':ACCOUNT})
    name, kwargs = next(c for c in calls if c[0] == 'lambda')
    cfg = kwargs['config']
    assert cfg.read_timeout == 280 and cfg.connect_timeout == 3
    assert cfg.retries['total_max_attempts'] == 1


def test_reader_can_invoke_only_own_strategy_service_and_cannot_train():
    kwargs = {'partition':'aws','region':'us-east-2','account':ACCOUNT}
    boundary = boundaries.env_permission_boundary('beta', **kwargs)
    policy = role_class_policy('beta','reader', **kwargs)
    def allowed(action, resource):
        return evaluate(Request(action, resource), {'identity':policy}, boundary).allowed
    assert allowed('lambda:InvokeFunction', ARN)
    assert allowed('lambda:InvokeFunction', ARN + ':$LATEST')
    for resource in (
        ARN + ':1',
        ARN + ':live',
        ARN.replace('beta','gamma'),
        ARN.replace('beta','gamma') + ':$LATEST',
        ARN.replace('inference','dispatcher'),
        ARN.replace('inference','dispatcher') + ':$LATEST',
    ):
        assert not allowed('lambda:InvokeFunction', resource), resource
    assert not allowed('sagemaker:CreateProcessingJob', '*')
    assert not allowed('ssm:PutParameter', f'arn:aws:ssm:us-east-2:{ACCOUNT}:parameter/finplan/beta/financemodel/config/advisory-policy')


def test_empty_request_flows_through_tool_pipeline_and_preserves_share_cash_evidence(offline, invoke):
    from finplan_contracts.schemas import contracts_root
    from finplan_tools.backends.jobs import JobClient
    fixture_path=next((contracts_root()/'fixtures/tools/recommend-portfolio-response/valid').glob('*saved*.json'))
    response=json.loads(fixture_path.read_text())
    # Consistent indicative sell: 20 shares at 450 to 10 shares, with proceeds left in cash.
    rec=response['recommendation']
    rec['target_weights']=[{'instrument_id':'VOO','weight':.45}]
    rec['cash_weight']=.55
    rec['decisions'][0].update(action='sell',delta_weight=-.45,indicative_notional=-4500.)
    client=Client(json.dumps(response).encode())
    offline.runtime.jobs=lambda timeout:JobClient(offline.jobs,timeout=timeout,strategy_client=backend(client))
    got=invoke(offline,'recommend_portfolio',{})
    assert got==response,got
    assert len(client.calls)==1 and json.loads(client.calls[0]['Payload'])['request']=={}
    assert got['recommendation']['decisions'][0]['delta_quantity']==-10
    assert got['recommendation']['cash_weight']==.55
    assert got['recommendation']['portfolio_state']['source']=='saved_paper'
    assert offline.jobs.count()==0 and offline.platform.count()==0


def test_default_recommendation_rejects_old_model_before_lambda_call(offline, invoke):
    from finplan_tools.backends.jobs import JobClient
    from finplan_tools_testing.runtime import release_manifest
    release=release_manifest('financemodel','beta');release['contract_version']='1.2.0'
    offline.set_param('financemodel','release','manifest',release)
    client=Client()
    offline.runtime.jobs=lambda timeout:JobClient(offline.jobs,timeout=timeout,strategy_client=backend(client))
    got=invoke(offline,'recommend_portfolio',{})
    assert got['code']=='DEPENDENCY_UNAVAILABLE' and got['details']['required_contract_version']=='1.3.0'
    assert not client.calls and offline.jobs.count()==0 and offline.platform.count()==0


def test_default_request_schema_supports_saved_book_without_mixing_actual_state():
    from finplan_tools.core.contracts import validate_document
    assert validate_document({},'tools/recommend-portfolio-invocation-request').valid
    assert validate_document({'portfolio_id':'pf_01KDVDNAZ83BAMMYCEGWF33DPM'},'tools/recommend-portfolio-invocation-request').valid
    assert not validate_document({'as_of':'2026-10-08'},'tools/recommend-portfolio-invocation-request').valid
    assert not validate_document({'holdings':{}},'tools/recommend-portfolio-invocation-request').valid
