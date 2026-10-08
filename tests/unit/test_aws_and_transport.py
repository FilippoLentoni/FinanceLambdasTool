"""Real-deploy lesson L4 (regional SigV4 clients, no bare S3 client) and the SigV4 producer transport."""

from __future__ import annotations

import io
import json
import re
import urllib.error
from pathlib import Path

import pytest

from finplan_tools.backends.sigv4 import SigV4HttpTransport, validate_endpoint
from finplan_tools.core import aws_clients
from finplan_tools.core.errors import ToolError
from finplan_tools.core.transport import CallMeta, ProducerRequest
from tests.offline_env import FAKE_ACCESS_KEY

ROOT = Path(__file__).resolve().parents[2]
REGION = "us-east-2"
PLAN_URL = "https://abc123.execute-api.us-east-2.amazonaws.com/beta/"


def test_s3_client_is_regional_sigv4():
    c = aws_clients.s3_client(REGION)
    assert c.meta.region_name == REGION
    assert c.meta.endpoint_url == f"https://s3.{REGION}.amazonaws.com"
    assert c.meta.config.signature_version == "s3v4"
    assert c.meta.config.s3["addressing_style"] == "virtual"


def test_ssm_client_is_regional():
    c = aws_clients.ssm_client(REGION)
    assert c.meta.region_name == REGION and c.meta.config.signature_version == "v4"


def test_no_bare_s3_client_anywhere():
    """Guard: ``boto3.client("s3")`` (SigV2 presign on the global host) broke the platform's first beta run."""
    pat = re.compile(r"""\.client\(\s*["']s3["']""")
    offenders = []
    for base in ("src", "infra", "scripts", "testing"):
        for p in (ROOT / base).rglob("*.py"):
            if p.name in ("aws_clients.py", "build_gates.py"):
                continue
            if pat.search(p.read_text(encoding="utf-8")):
                offenders.append(str(p.relative_to(ROOT)))
    assert offenders == []


def test_offline_credentials_are_fake():
    assert aws_clients.credentials().access_key == FAKE_ACCESS_KEY


# ---------------------------------------------------------------- endpoint validation
@pytest.mark.parametrize("url", [PLAN_URL, "https://abc123.execute-api.us-east-2.amazonaws.com/beta/v1/ingestions", "https://abc123.execute-api.us-east-2.amazonaws.com/prod"])
def test_valid_endpoints(url):
    assert validate_endpoint(url, REGION) == url.rstrip("/")


@pytest.mark.parametrize("url", ["http://abc.execute-api.us-east-2.amazonaws.com/beta", "https://abc.execute-api.us-west-2.amazonaws.com/beta", "https://evil.example/beta", "", "https://abc.execute-api.us-east-2.amazonaws.com/beta?x=1"])
def test_invalid_endpoints(url):
    with pytest.raises(ToolError) as e:
        validate_endpoint(url, REGION)
    assert e.value.code == "DEPENDENCY_UNAVAILABLE"


# ---------------------------------------------------------------- signing and transport
class _Resp(io.BytesIO):
    def __init__(self, status, body):
        super().__init__(json.dumps(body).encode())
        self.status = status
        self.headers = {"Content-Type": "application/json"}

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _transport(url=PLAN_URL):
    return SigV4HttpTransport(lambda: url, region=REGION, credentials=aws_clients.credentials, producer="financialplanning")


def test_request_is_sigv4_signed_with_transport_headers(monkeypatch):
    seen = {}

    def fake_urlopen(req, timeout):
        seen.update(url=req.full_url, headers={k.lower(): v for k, v in req.header_items()}, data=req.data, timeout=timeout, method=req.get_method())
        return _Resp(200, {"ok": True})

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    meta = CallMeta("corr-sig-000001", "1.0.0", {"channel": "direct_test", "correlation_id": "corr-sig-000001"})
    resp = _transport().send(ProducerRequest("POST", "v1/plans/pl_X/publications", {"a": None, "b": "1"}, {"z": 1, "a": 2}, meta.headers(), 7))
    assert resp.status == 200 and resp.body == {"ok": True}
    assert seen["url"] == "https://abc123.execute-api.us-east-2.amazonaws.com/beta/v1/plans/pl_X/publications?b=1"
    auth = seen["headers"]["authorization"]
    assert auth.startswith("AWS4-HMAC-SHA256 ") and f"/{REGION}/execute-api/aws4_request" in auth
    assert seen["headers"]["x-correlation-id"] == "corr-sig-000001" and "x-finplan-caller" in seen["headers"]
    assert seen["data"] == b'{"a":2,"z":1}' and seen["timeout"] == 7 and seen["method"] == "POST"


def test_empty_path_targets_published_url(monkeypatch):
    seen = {}
    monkeypatch.setattr("urllib.request.urlopen", lambda req, timeout: seen.setdefault("url", req.full_url) and _Resp(200, {}))
    _transport("https://abc123.execute-api.us-east-2.amazonaws.com/beta/v1/ingestions").send(ProducerRequest("POST", "", body={}))
    assert seen["url"].endswith("/beta/v1/ingestions")


def test_http_error_is_returned_for_mapping(monkeypatch):
    def fake(req, timeout):
        raise urllib.error.HTTPError(req.full_url, 409, "Conflict", {}, io.BytesIO(json.dumps({"code": "CONFLICT"}).encode()))

    monkeypatch.setattr("urllib.request.urlopen", fake)
    resp = _transport().send(ProducerRequest("GET", "v1/plans/x"))
    assert resp.status == 409 and resp.body == {"code": "CONFLICT"}


def test_unreachable_is_dependency_unavailable(monkeypatch):
    def fake(req, timeout):
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr("urllib.request.urlopen", fake)
    with pytest.raises(ToolError) as e:
        _transport().send(ProducerRequest("GET", "v1/plans/x"))
    assert e.value.code == "DEPENDENCY_UNAVAILABLE" and e.value.retryable is True


def test_absent_reference_is_dependency_unavailable(no_network):
    with pytest.raises(ToolError) as e:
        _transport(None).send(ProducerRequest("GET", "v1/plans/x"))
    assert e.value.code == "DEPENDENCY_UNAVAILABLE"


def test_job_client_has_no_approve_or_cancel():
    """EXP-07: no tool code path can approve or cancel a run."""
    from finplan_tools.backends.jobs import JobClient

    assert not [m for m in dir(JobClient) if re.search(r"approve|cancel", m)]
    for p in (ROOT / "src" / "finplan_tools").rglob("*.py"):
        text = p.read_text(encoding="utf-8")
        assert not re.search(r"""["'][^"']*/(approve|cancel)["']""", text), p
