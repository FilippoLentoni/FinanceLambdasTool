"""Public audit identity projection; immutable producer evidence stays unchanged."""
from __future__ import annotations

import hashlib
from collections.abc import Mapping

from .artifacts import response_leaks

_CALLER_FIELDS = frozenset({"subject_hash", "roles", "channel", "correlation_id", "synthetic", "role_class", "principal_hash"})
_ON_BEHALF_FIELDS = frozenset({"subject_hash", "roles", "channel", "correlation_id", "synthetic"})


def _caller(value):
    # Platform audit_view contains the authenticated IAM principal plus a trusted
    # on-behalf-of caller. Public evidence needs a stable join, not account or role ARNs.
    out = {key: public_document(item) for key, item in value.items() if key in _CALLER_FIELDS}
    principal = value.get("principal")
    if isinstance(principal, str):
        if response_leaks({"principal": principal}):
            out["principal_hash"] = "sha256:" + hashlib.sha256(principal.encode()).hexdigest()
        else:
            out["principal"] = principal  # Nonprivate synthetic/operator labels remain readable.
    if isinstance(value.get("on_behalf_of"), Mapping):
        out["on_behalf_of"] = {key: public_document(item) for key, item in value["on_behalf_of"].items() if key in _ON_BEHALF_FIELDS}
    return out


def public_document(value):
    """Copy a response, projecting every nested caller block consistently.

    Resolution fields, prices, fills, revisions, financial metrics and artifact
    checksums are retained exactly. Checksums still identify the immutable producer
    artifact, which contains the original private audit identity. The normal output
    contract and leak guard continue to validate the projected response.
    """
    if isinstance(value, Mapping):
        return {key: _caller(item) if key == "caller" and isinstance(item, Mapping) else public_document(item) for key, item in value.items()}
    if isinstance(value, list):
        return [public_document(item) for item in value]
    return value
