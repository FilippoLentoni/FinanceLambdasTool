"""Verify the transport-only Cognito access token for paid classical MCP research.

AgentCore forwards the allowlisted header in client_context.custom, never tool arguments:
https://aws.amazon.com/blogs/security/identity-aware-ai-data-agents-with-aws-lake-formation-and-trusted-identity-propagation/
Only the same-environment SSM authorizer metadata and pool reference choose the issuer/JWKS.
Unverified JWT headers select a cached key ID; they never choose an algorithm or network URL.
"""
from __future__ import annotations

import hashlib
import json
import re
import threading
import time
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable, Mapping

import jwt

from .errors import FORBIDDEN, ToolError
from .gateway import _custom

TOKEN_HEADER = "X-Finplan-User-Token"
PROPAGATED_HEADERS = "bedrockAgentCorePropagatedHeaders"
_POOL = re.compile(r"^[a-z]{2}(?:-[a-z]+)+-\d_[A-Za-z0-9]{1,64}$")
_GROUP = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def fetch_jwks(url: str) -> dict[str, Any]:
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.build_opener(_NoRedirect()).open(request, timeout=3) as response:  # noqa: S310 - URL built from pinned Cognito issuer only
        content = response.read(65537)
    if len(content) > 65536:
        raise ValueError("oversized JWKS")
    return json.loads(content)


@dataclass(frozen=True)
class VerifiedResearchCaller:
    identity: str
    groups: frozenset[str]


class GatewayResearchAuthorizer:
    """Bounded key cache; all failures deny paid compute with a token-free error."""

    def __init__(self, references: Any, region: str, *, fetch: Callable = fetch_jwks, clock: Callable = time.monotonic):
        self.references, self.region, self.fetch, self.clock = references, region, fetch, clock
        self._cache: tuple[str, float, dict[str, Any]] | None = None
        self._lock = threading.Lock()

    def verify_context(self, context: Any) -> VerifiedResearchCaller:
        try:
            custom = _custom(context) or {}
            headers = custom.get(PROPAGATED_HEADERS)
            if not isinstance(headers, Mapping):
                raise ValueError("missing propagated headers")
            tokens = [v for k, v in headers.items() if isinstance(k, str) and k.lower() == TOKEN_HEADER.lower()]
            if len(tokens) != 1 or not isinstance(tokens[0], str) or not 1 <= len(tokens[0]) <= 16384:
                raise ValueError("missing or malformed propagated token")
            token = tokens[0]
            header = jwt.get_unverified_header(token)
            if header.get("alg") != "RS256" or not isinstance(header.get("kid"), str) or not 1 <= len(header["kid"]) <= 256:
                raise ValueError("unsupported signing key")
            if set(header).intersection({"jku", "jwk", "x5u", "x5c", "crit", "b64"}):
                raise ValueError("untrusted key-selection header")
            metadata = self.references.get_json("financeagent", "agent", "authorizer-metadata-ref")
            pool = self.references.get("financeagent", "agent", "user-pool-ref")
            if not isinstance(metadata, dict) or not isinstance(pool, str) or not _POOL.fullmatch(pool) or not pool.startswith(self.region + "_"):
                raise ValueError("invalid pinned authorizer metadata")
            issuer = f"https://cognito-idp.{self.region}.amazonaws.com/{pool}"
            clients = metadata.get("allowed_clients")
            if metadata.get("issuer") != issuer or not isinstance(clients, list) or not 1 <= len(clients) <= 10 or any(not isinstance(c, str) or not 1 <= len(c) <= 128 for c in clients):
                raise ValueError("invalid pinned issuer or clients")
            key = self._key(issuer + "/.well-known/jwks.json", header["kid"])
            claims = jwt.decode(token, key, algorithms=["RS256"], issuer=issuer,
                                options={"require": ["exp", "iat", "iss", "sub", "client_id", "token_use"], "verify_aud": False})
            if claims["token_use"] != "access" or claims["client_id"] not in clients:
                raise ValueError("wrong token type or client")
            if not isinstance(claims["sub"], str) or not 1 <= len(claims["sub"]) <= 256:
                raise ValueError("invalid subject")
            if any(not isinstance(claims[name], int) or isinstance(claims[name], bool) for name in ("exp", "iat", "nbf") if name in claims):
                raise ValueError("invalid token timestamp")
            scopes = metadata.get("scopes") or {}
            ci_scope = scopes.get("ci_test", "finplan-agent/ci_test")
            if not isinstance(claims.get("scope", ""), str) or ci_scope in claims.get("scope", "").split():
                raise ValueError("CI credentials cannot authorize paid compute")
            groups = claims.get("cognito:groups", [])
            if not isinstance(groups, list) or len(groups) > 64 or any(not isinstance(g, str) or not _GROUP.fullmatch(g) for g in groups):
                raise ValueError("invalid signed groups")
            identity = "cognito:" + hashlib.sha256((issuer + "|" + claims["sub"]).encode()).hexdigest()
            return VerifiedResearchCaller(identity, frozenset(groups))
        except Exception:
            # Never include token bytes, JWT claims or a decoder exception in logs/responses.
            raise ToolError(FORBIDDEN, "paid research requires a valid propagated user access token", reason="verified_user_token_required") from None

    def _key(self, url: str, kid: str) -> Any:
        with self._lock:
            now = self.clock()
            cached = self._cache
            fresh = cached is not None and cached[0] == url and now - cached[1] < 300
            if fresh and kid in cached[2]:
                return cached[2][kid]
            # Unknown IDs cannot force an unbounded refresh flood; normal rotation retries after 30s.
            if fresh and now - cached[1] < 30:
                raise ValueError("unknown signing key")
            document = self.fetch(url)
            rows = document.get("keys") if isinstance(document, dict) else None
            if not isinstance(rows, list) or not 1 <= len(rows) <= 10:
                raise ValueError("invalid JWKS")
            keys = {}
            for row in rows:
                if not isinstance(row, dict) or row.get("kty") != "RSA" or row.get("alg") != "RS256" or row.get("use") != "sig" or not isinstance(row.get("kid"), str) or not 1 <= len(row["kid"]) <= 256:
                    raise ValueError("invalid signing key")
                if row["kid"] in keys or "d" in row:
                    raise ValueError("duplicate or private signing key")
                key = jwt.algorithms.RSAAlgorithm.from_jwk(row)
                if key.key_size < 2048:
                    raise ValueError("weak RSA key")
                keys[row["kid"]] = key
            self._cache = (url, now, keys)
            if kid not in keys:
                raise ValueError("unknown signing key")
            return keys[kid]
