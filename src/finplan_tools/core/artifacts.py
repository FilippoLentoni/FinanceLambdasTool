"""Trusted artifact references only (spec tool-request-handling, TRH-09; CS-08).

Requests: storage URIs (``s3://`` and any other scheme), ARNs, S3 endpoints and path-like values
are rejected before any other work (:func:`storage_input_problems`), in addition to the contract
``no_storage_locations`` check every tool request schema declares. Field names that only make sense
as storage locations (``bucket``, ``s3_key``, ``output_location`` ...) are rejected too.

Responses: :func:`response_leaks` lists every string that looks like a bucket, object key, ARN,
account ID or endpoint URL. The pipeline refuses to send such a response (``INTERNAL``); stored
artifacts leave a tool only as contract ``core/v1/artifact-ref.json`` references or as
platform-issued time-limited download grants at pointers a tool explicitly declares.
"""

from __future__ import annotations

import re
from typing import Any, Iterable, Iterator

__all__ = ["storage_input_problems", "response_leaks", "STORAGE_FIELD_NAMES"]

_ID_BASE = "https://contracts.finplan.invalid/"
_URI = re.compile(r"(?i)^\s*[a-z][a-z0-9+.-]*://")
_ARN = re.compile(r"(?i)\barn:[a-z0-9-]*:")
_S3_HOST = re.compile(r"(?i)\.s3[.-]([a-z0-9-]+\.)?amazonaws\.com|\bs3://")
_PATH_LIKE = re.compile(r"^\s*(/|~/|\.{1,2}/)|\\")
_ACCOUNT_ID = re.compile(r"(?<![0-9A-Za-z_.])[0-9]{12}(?![0-9A-Za-z_]|\.[0-9])")
_ENDPOINT = re.compile(r"(?i)\bhttps?://|\b[a-z0-9-]+\.execute-api\.[a-z0-9-]+\.amazonaws\.com|\.amazonaws\.com\b")
_BUCKET_LIKE = re.compile(r"(?i)\b[a-z0-9][a-z0-9.-]{2,61}[a-z0-9]/(?:[^\s/]+/)+[^\s/]+\.(?:json|parquet|csv|zip|xlsx|gz|npz|pkl)\b")
STORAGE_FIELD_NAMES = frozenset(
    {"bucket", "bucket_name", "s3_bucket", "s3_key", "s3_uri", "object_key", "key_prefix", "output_location", "input_location", "output_uri", "input_uri", "storage_uri", "file_path", "path", "uri", "url"}
)


def _walk(doc: Any, path: tuple[Any, ...] = ()) -> Iterator[tuple[tuple[Any, ...], Any, Any]]:
    """Yield (path, key, value) for every value (key is the last mapping key on the path)."""
    if isinstance(doc, dict):
        for k, v in doc.items():
            yield path + (k,), k, v
            yield from _walk(v, path + (k,))
    elif isinstance(doc, list):
        for i, v in enumerate(doc):
            yield path + (i,), None, v
            yield from _walk(v, path + (i,))


def _pointer(path: Iterable[Any]) -> str:
    return "".join("/" + str(p).replace("~", "~0").replace("/", "~1") for p in path)


def storage_input_problems(request: Any) -> list[str]:
    """JSON pointers of caller-chosen storage locations in a request (empty when clean)."""
    out: list[str] = []
    for path, key, value in _walk(request):
        if isinstance(key, str) and key.lower() in STORAGE_FIELD_NAMES:
            out.append(_pointer(path))
            continue
        if isinstance(value, str) and not value.startswith(_ID_BASE):
            if _URI.search(value) or _ARN.search(value) or _S3_HOST.search(value) or _PATH_LIKE.search(value):
                out.append(_pointer(path))
    return sorted(set(out))


def response_leaks(response: Any, *, allowed_pointers: Iterable[str] = ()) -> list[str]:
    """JSON pointers of strings in a response that look like storage locations or account data.

    ``allowed_pointers`` are pointer prefixes where a tool deliberately returns platform-issued
    time-limited download grants (none of the tool schemas in contracts 1.0.0 has one).
    """
    allowed = tuple(allowed_pointers)
    out: list[str] = []
    for path, _key, value in _walk(response):
        if not isinstance(value, str) or value.startswith(_ID_BASE):
            continue
        ptr = _pointer(path)
        if any(ptr == a or ptr.startswith(a.rstrip("/") + "/") for a in allowed):
            continue
        if _S3_HOST.search(value) or _ARN.search(value) or _ACCOUNT_ID.search(value) or _ENDPOINT.search(value) or _BUCKET_LIKE.search(value):
            out.append(ptr)
    return out
