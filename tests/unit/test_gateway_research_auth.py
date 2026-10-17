"""Signed user identity is required before any paid MCP producer call."""
from __future__ import annotations

import json
import time
from types import SimpleNamespace

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from finplan_contracts import boundaries
from finplan_contracts.iam import Request, evaluate

from finplan_tools.core.errors import ToolError
from finplan_tools.core.gateway_research_auth import GatewayResearchAuthorizer
from finplan_tools.handler import invoke
from infra.stacks.policies import role_class_policy

from .test_classical_tools import AID, Client, evidence, wire

REGION = "us-east-2"
POOL = REGION + "_FAKEPOOL"
ISSUER = "https://cognito-idp." + REGION + ".amazonaws.com/" + POOL
METADATA = {"issuer": ISSUER, "allowed_clients": ["public-client", "machine-client"], "public_client_id": "public-client", "scopes": {"ci_test": "finplan-agent/ci_test"}}
REQUEST = {"review_id": AID, "dry_run": False, "confirmed_by_user": True, "idempotency_key": "weekly-user-intent"}


@pytest.fixture(scope="module")
def signing_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def access_token(key, **changes):
    now = int(time.time())
    claims = {"iss": ISSUER, "sub": "test-user", "client_id": "public-client", "token_use": "access", "iat": now - 5, "exp": now + 600, "scope": "finplan-agent/invoke", "cognito:groups": ["researcher"]}
    claims.update(changes)
    return jwt.encode(claims, key, algorithm="RS256", headers={"kid": "test-key"})


def context(token=None, **headers):
    values = {} if token is None else {"X-Finplan-User-Token": token}
    values.update(headers)
    return SimpleNamespace(client_context=SimpleNamespace(custom={"bedrockAgentCoreToolName": "classical___run_portfolio_research", "bedrockAgentCorePropagatedHeaders": values}))


def authorizer(key, *, metadata=None, pool=POOL, fetches=None, clock=time.monotonic):
    jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key()))
    jwk.update(kid="test-key", alg="RS256", use="sig")
    def fetch(url):
        if fetches is not None:
            fetches.append(url)
        assert url == ISSUER + "/.well-known/jwks.json"
        return {"keys": [jwk]}
    refs = SimpleNamespace(get_json=lambda *args: METADATA if metadata is None else metadata, get=lambda *args: pool)
    return GatewayResearchAuthorizer(refs, REGION, fetch=fetch, clock=clock)


def test_verified_researcher_has_signed_groups_and_hashed_identity(signing_key):
    calls = []
    verifier = authorizer(signing_key, fetches=calls)
    user = verifier.verify_context(context(access_token(signing_key)))
    assert user.groups == frozenset({"researcher"})
    assert user.identity.startswith("cognito:") and "test-user" not in user.identity
    assert verifier.verify_context(context(access_token(signing_key))).identity == user.identity
    assert calls == [ISSUER + "/.well-known/jwks.json"]


@pytest.mark.parametrize("changes", [
    {"exp": 1}, {"nbf": int(time.time()) + 600}, {"iat": int(time.time()) + 600},
    {"iss": ISSUER + "other"}, {"client_id": "foreign-client"}, {"token_use": "id"},
    {"sub": ""}, {"cognito:groups": "researcher"}, {"exp": True},
    {"scope": "finplan-agent/ci_test", "client_id": "machine-client"},
])
def test_bad_signed_claims_fail_closed_without_token_in_error(signing_key, changes):
    token = access_token(signing_key, **changes)
    with pytest.raises(ToolError) as error:
        authorizer(signing_key).verify_context(context(token))
    assert error.value.code == "FORBIDDEN"
    assert token not in str(error.value)


@pytest.mark.parametrize("token", [None, "invalid", "a.b.c", "x" * 16385])
def test_missing_or_malformed_token_does_not_fetch_keys(signing_key, token):
    calls = []
    with pytest.raises(ToolError):
        authorizer(signing_key, fetches=calls).verify_context(context(token))
    assert not calls


def test_wrong_signature_algorithm_and_untrusted_key_url_fail_closed(signing_key):
    claims = jwt.decode(access_token(signing_key), options={"verify_signature": False})
    wrong_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    invalid = [
        jwt.encode(claims, "a" * 32, algorithm="HS256", headers={"kid": "test-key"}),
        jwt.encode(claims, wrong_key, algorithm="RS256", headers={"kid": "test-key"}),
        jwt.encode(claims, signing_key, algorithm="RS256", headers={"kid": "test-key", "jku": "https://attacker.invalid/jwks"}),
        jwt.encode(claims, None, algorithm="none", headers={"kid": "test-key"}),
    ]
    for token in invalid:
        with pytest.raises(ToolError):
            authorizer(signing_key).verify_context(context(token))


def test_wrong_environment_or_untrusted_metadata_cannot_choose_jwks(signing_key):
    for metadata, pool in [({**METADATA, "issuer": ISSUER.replace(POOL, REGION + "_OTHER")}, POOL), ({**METADATA, "issuer": "https://attacker.invalid"}, POOL), (METADATA, "us-west-2_OTHER")]:
        calls = []
        with pytest.raises(ToolError):
            authorizer(signing_key, metadata=metadata, pool=pool, fetches=calls).verify_context(context(access_token(signing_key)))
        assert not calls


def test_duplicate_case_insensitive_header_is_rejected(signing_key):
    token = access_token(signing_key)
    with pytest.raises(ToolError):
        authorizer(signing_key).verify_context(context(token, **{"x-finplan-user-token": token}))


def test_unknown_key_refresh_is_bounded(signing_key):
    clock, calls = [0.0], []
    verifier = authorizer(signing_key, fetches=calls, clock=lambda: clock[0])
    verifier.verify_context(context(access_token(signing_key)))
    claims = jwt.decode(access_token(signing_key), options={"verify_signature": False})
    unknown = jwt.encode(claims, signing_key, algorithm="RS256", headers={"kid": "unknown"})
    for _ in range(3):
        with pytest.raises(ToolError):
            verifier.verify_context(context(unknown))
    assert len(calls) == 1
    clock[0] = 31
    with pytest.raises(ToolError):
        verifier.verify_context(context(unknown))
    assert len(calls) == 2


def test_signed_researcher_reaches_paid_preflight_and_identity_headers(offline, signing_key):
    client = Client(evidence("research_run"))
    wire(offline, client)
    offline.runtime.gateway_research_authorizer = authorizer(signing_key)
    token = access_token(signing_key)
    response = invoke("run_portfolio_research", REQUEST, context(token), offline.runtime)
    assert "code" not in response, response
    payloads = [json.loads(call["Payload"]) for call in client.calls]
    assert len(payloads) == 2 and payloads[0]["request"]["dry_run"] is True and payloads[1]["request"]["dry_run"] is False
    caller = json.loads(payloads[1]["headers"]["X-Finplan-Caller"])
    assert caller["channel"] == "hosted_agent" and caller["subject_hash"].startswith("sha256:")
    assert all(token not in json.dumps(payload) for payload in payloads)


@pytest.mark.parametrize("groups", [[], ["viewer"], ["plan_publisher"]])
def test_signed_nonresearcher_never_calls_model(offline, signing_key, groups):
    client = Client(evidence("research_run"))
    wire(offline, client)
    offline.runtime.gateway_research_authorizer = authorizer(signing_key)
    response = invoke("run_portfolio_research", REQUEST, context(access_token(signing_key, **{"cognito:groups": groups})), offline.runtime)
    assert response["code"] == "FORBIDDEN" and not client.calls


def test_argument_claims_cannot_spoof_gateway_researcher(offline, signing_key):
    client = Client(evidence("research_run"))
    wire(offline, client)
    offline.runtime.gateway_research_authorizer = authorizer(signing_key)
    response = invoke("run_portfolio_research", {**REQUEST, "caller": "researcher"}, context(), offline.runtime)
    assert response["code"] == "FORBIDDEN" and not client.calls


def test_dry_run_never_demands_or_decodes_transport_token(offline):
    client = Client(evidence("research_run"))
    wire(offline, client)
    response = invoke("run_portfolio_research", {"review_id": AID, "dry_run": True}, context("invalid"), offline.runtime)
    assert "code" not in response and len(client.calls) == 1


def test_public_auth_references_are_only_readable_by_same_environment_submitter():
    kw = {"partition": "aws", "region": REGION, "account": "1" * 12}
    boundary = boundaries.env_permission_boundary("beta", **kw)
    for role in ("reader", "submitter", "plan-writer"):
        policy = role_class_policy("beta", role, **kw)
        for env in ("beta", "gamma"):
            for name in ("authorizer-metadata-ref", "user-pool-ref"):
                arn = "arn:aws:ssm:" + REGION + ":" + kw["account"] + ":parameter/finplan/" + env + "/financeagent/agent/" + name
                allowed = evaluate(Request("ssm:GetParameter", arn), {"identity": policy}, boundary).allowed
                assert allowed is (role in ("submitter", "plan-writer") and env == "beta")
