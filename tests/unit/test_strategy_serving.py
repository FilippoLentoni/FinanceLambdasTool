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
    assert not allowed('lambda:InvokeFunction', ARN.replace('beta','gamma'))
    assert not allowed('lambda:InvokeFunction', ARN.replace('inference','dispatcher'))
    assert not allowed('sagemaker:CreateProcessingJob', '*')
    assert not allowed('ssm:PutParameter', f'arn:aws:ssm:us-east-2:{ACCOUNT}:parameter/finplan/beta/financemodel/config/advisory-policy')
