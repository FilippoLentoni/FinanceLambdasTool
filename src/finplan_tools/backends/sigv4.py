"""SigV4-signed HTTPS transport to a same-environment producer API (API Gateway REST, IAM auth).

The endpoint is never configured in code, environment variables or IaC: :class:`SigV4HttpTransport`
asks a callable (normally a :class:`~finplan_tools.core.references.ReferenceResolver` method such
as ``plan_endpoint``) for the base URL on every call; the resolver caches it for the reference TTL.
An absent reference is ``DEPENDENCY_UNAVAILABLE``. The request is signed for ``execute-api`` in the
Lambda's region with the Lambda role's credentials.

Transport failures: connection errors and timeouts -> ``DEPENDENCY_UNAVAILABLE`` (retryable);
HTTP responses of any status are returned to the typed client, which maps them.
"""

from __future__ import annotations

import json
import re
import socket
import urllib.error
import urllib.request
from typing import Any, Callable
from urllib.parse import quote, urlencode

from ..core.errors import ToolError
from ..core.transport import ProducerRequest, ProducerResponse

__all__ = ["SigV4HttpTransport", "validate_endpoint"]

_ENDPOINT = re.compile(r"^https://[a-z0-9-]+\.execute-api\.(?P<region>[a-z]{2}(-gov)?-[a-z]+-[0-9])\.amazonaws\.com(/[A-Za-z0-9_-]+)*/?\Z")
_MAX_RESPONSE = 6 * 1024 * 1024


def validate_endpoint(url: str, region: str) -> str:
    """Accept only a regional API Gateway HTTPS URL in ``region``; returns it without a trailing '/'.

    The platform publishes ``plan-endpoint`` as the stage invoke URL and ``ingestion-endpoint`` as
    the full ``.../v1/ingestions`` URL; FinanceModel publishes ``job-endpoint`` as the stage URL.
    """
    m = _ENDPOINT.match(url or "")
    if not m or m.group("region") != region:
        raise ToolError.dependency_unavailable("the producer endpoint reference is not a regional API endpoint", reason="bad_endpoint_reference")
    return url.rstrip("/")


class SigV4HttpTransport:
    def __init__(self, endpoint: Callable[[], str | None], *, region: str, credentials: Callable[[], Any], producer: str, service: str = "execute-api") -> None:
        self._endpoint = endpoint
        self.region = region
        self._credentials = credentials
        self.producer = producer
        self.service = service

    def _url(self, request: ProducerRequest) -> str:
        base = self._endpoint()
        if not base:
            raise ToolError.dependency_unavailable(f"the {self.producer} API is not published in this environment", producer=self.producer)
        url = validate_endpoint(base, self.region)
        if request.path.strip("/"):  # an empty path targets the published URL itself (ingestion-endpoint)
            url += "/" + "/".join(quote(p, safe="") for p in request.path.strip("/").split("/"))
        query = {k: v for k, v in request.query.items() if v is not None}
        if query:
            url += "?" + urlencode(sorted(query.items()), doseq=True)
        return url

    def send(self, request: ProducerRequest) -> ProducerResponse:
        from botocore.auth import SigV4Auth
        from botocore.awsrequest import AWSRequest

        url = self._url(request)
        data = None if request.body is None else json.dumps(request.body, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        headers = {"Content-Type": "application/json", "Accept": "application/json", **dict(request.headers)}
        aws_req = AWSRequest(method=request.method.upper(), url=url, data=data, headers=headers)
        SigV4Auth(self._credentials(), self.service, self.region).add_auth(aws_req)
        prepared = aws_req.prepare()
        http_req = urllib.request.Request(prepared.url, data=data, headers=dict(prepared.headers.items()), method=request.method.upper())
        try:
            with urllib.request.urlopen(http_req, timeout=request.timeout_seconds) as resp:  # noqa: S310 - https only (validated)
                status, raw, rh = resp.status, resp.read(_MAX_RESPONSE + 1), dict(resp.headers.items())
        except urllib.error.HTTPError as err:
            status, raw, rh = err.code, err.read(_MAX_RESPONSE + 1), dict(err.headers.items()) if err.headers else {}
        except (urllib.error.URLError, socket.timeout, TimeoutError, ConnectionError) as exc:
            raise ToolError.dependency_unavailable(f"the {self.producer} API is unreachable", producer=self.producer, reason=type(exc).__name__) from None
        if len(raw) > _MAX_RESPONSE:
            raise ToolError.internal("the producer response is too large", producer=self.producer)
        try:
            body = json.loads(raw) if raw else None
        except (json.JSONDecodeError, UnicodeDecodeError):
            body = None
        return ProducerResponse(status=status, body=body, headers={k.lower(): v for k, v in rh.items()})
