"""Durable, sanitized tool receipts, independent of bounded CloudWatch summaries."""
from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping

from .contracts import contract_version
from .identity import caller_block
from .transport import CallMeta

_SECRET_KEYS = re.compile(r"authorization|bearer|password|secret|access.token|refresh.token|id.token|user.token|download.grant|presigned|credential", re.I)
_JWT = re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b")
_PRIVATE_LOCATION = re.compile(r"s3://[^\s\"]+|arn:[a-z0-9-]*:[^\s\"]+|https?://[^\s\"]*amazonaws\.com[^\s\"]*", re.I)
_SIGNED_URL = re.compile(r"https?://[^\s\"]*(?:X-Amz-|Signature=)[^\s\"]*", re.I)
_ACTIVITY_METADATA = ("activity_event_id", "checksum", "event_kind", "recorded_at", "portfolio_id", "decision_id", "input_snapshot_id", "session_id", "correlation_id", "caller", "contract_version", "synthetic")


def sanitize(value):
    if isinstance(value, Mapping):
        return {str(k): "<redacted>" if _SECRET_KEYS.search(str(k)) else sanitize(v) for k, v in value.items()}
    if isinstance(value, list):
        return [sanitize(v) for v in value]
    if isinstance(value, str):
        return _PRIVATE_LOCATION.sub("<redacted-private-location>", _SIGNED_URL.sub("<redacted-download-grant>", _JWT.sub("<redacted-token>", value)))
    return value


def activity_history_references(response):
    """Keep immutable evidence joins instead of copying an archive into its own history.

    The public MCP result remains unchanged. Only the new receipt stores references to
    already persisted originals, including their checksums and the original page cursor.
    """
    if not isinstance(response, Mapping) or "events" not in response:
        return response
    events = []
    for event in response["events"]:
        if not isinstance(event, Mapping) or not event.get("activity_event_id") or not event.get("checksum"):
            raise ValueError("activity history lacks an immutable event identifier or checksum")
        metadata = {key: event[key] for key in _ACTIVITY_METADATA if key in event}
        payload = event.get("payload") or event.get("summary") or {}
        metadata["summary"] = {key: payload[key] for key in ("tool", "status", "release_id", "graph_version") if key in payload}
        if payload.get("representation") == "chunked_immutable_json":
            metadata["summary"].update({key: payload[key] for key in ("representation", "offset", "end_offset", "total", "payload_checksum") if key in payload})
        events.append(metadata)
    return {**response, "events": events, "evidence_representation": "immutable_activity_references"}


def persist_tool_receipt(runtime, spec, invocation, cid, request, response, outcome):
    meta = CallMeta(cid, contract_version(), caller_block(invocation, cid) if invocation else None)
    archived_response = activity_history_references(response) if spec.name == "list_agent_activity" else response
    payload = {"tool": spec.name, "request": sanitize(request), "response": sanitize(archived_response), "status": outcome,
               "release_id": runtime.settings.release_id, "contract_version": contract_version(),
               "caller_identity": invocation.identity if invocation else "unresolved"}
    body = {"event_kind": "tool_invocation", "correlation_id": cid, "payload": payload,
            "idempotency_key": "receipt-" + hashlib.sha256((cid + "|" + spec.name).encode()).hexdigest()}
    # Link calls by the authoritative returned book or decision, not only caller hints.
    sources = [request, response or {}, (response or {}).get("recommendation", {}), (response or {}).get("decision", {})]
    for source in list(sources):
        if isinstance(source, Mapping) and isinstance(source.get("portfolio_state"), Mapping):
            sources.append(source["portfolio_state"])
    for key in ("portfolio_id", "decision_id", "input_snapshot_id"):
        for source in reversed(sources):
            if isinstance(source, Mapping) and source.get(key):
                body[key] = source[key]
                break
    runtime.platform(10).record_agent_activity(body, meta)
