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

import json
import re
from collections.abc import Iterable, Iterator
from typing import Any

__all__ = ["STORAGE_FIELD_NAMES", "response_leaks", "storage_input_problems"]

_ID_BASE = "https://contracts.finplan.invalid/"
_URI = re.compile(r"(?i)^\s*[a-z][a-z0-9+.-]*://")
_ARN = re.compile(r"(?i)\barn:[a-z0-9-]*:")
_S3_HOST = re.compile(r"(?i)\.s3[.-]([a-z0-9-]+\.)?amazonaws\.com|\bs3://")
_PATH_LIKE = re.compile(r"^\s*(/|~/|\.{1,2}/)|\\")
_ACCOUNT_ID = re.compile(r"(?<![0-9A-Za-z_.])[0-9]{12}(?![0-9A-Za-z_]|\.[0-9])")
_ENDPOINT = re.compile(r"(?i)\bhttps?://|\b[a-z0-9-]+\.execute-api\.[a-z0-9-]+\.amazonaws\.com|\.amazonaws\.com\b")
_BUCKET_LIKE = re.compile(r"(?i)\b[a-z0-9][a-z0-9.-]{2,61}[a-z0-9]/(?:[^\s/]+/)+[^\s/]+\.(?:json|parquet|csv|zip|xlsx|gz|npz|pkl)\b")
_GDELT_VERSIONS = frozenset({'gdelt-gkg-v1-organization-tone-v1', 'gdelt-gkg-v2-premarket-organization-tone-v1'})
_GDELT_ARCHIVE = re.compile(r'https://data\.gdeltproject\.org/(?:gkg/[0-9]{8}|gdeltv2/[0-9]{14})\.gkg\.csv\.zip\Z')
_GDELT_ATTRIBUTION = 'The GDELT Project, https://www.gdeltproject.org/'
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
        if isinstance(value, str) and not value.startswith(_ID_BASE) and (_URI.search(value) or _ARN.search(value) or _S3_HOST.search(value) or _PATH_LIKE.search(value)):
            out.append(_pointer(path))
    return sorted(set(out))


def response_leaks(response: Any, *, allowed_pointers: Iterable[str] = ()) -> list[str]:
    """JSON pointers of strings in a response that look like storage locations or account data.

    ``allowed_pointers`` are pointer prefixes where a tool deliberately returns platform-issued
    time-limited download grants (none of the tool schemas in contracts 1.0.0 has one).
    """
    allowed = tuple(allowed_pointers)
    public_gdelt = _gdelt_citation_pointers(response)
    out: list[str] = []
    for path, _key, value in _walk(response):
        if not isinstance(value, str) or value.startswith(_ID_BASE):
            continue
        ptr = _pointer(path)
        if ptr in public_gdelt:
            continue
        if any(ptr == a or ptr.startswith(a.rstrip("/") + "/") for a in allowed):
            continue
        if _S3_HOST.search(value) or _ARN.search(value) or _ACCOUNT_ID.search(value) or _ENDPOINT.search(value) or _BUCKET_LIKE.search(value):
            out.append(ptr)
    return out


def _gdelt_citation_pointers(response: Any) -> set[str]:
    """Exact public GDELT citations inside registered numeric-source evidence only.

    These are public source/rights citations, not object-store or download grants.
    Exempt individual validated leaves; never exempt a provenance subtree. Frozen
    canonical document strings must decode to the identical structured document and
    pass the same complete scan, including every non-citation field.
    """
    allowed = set()

    def lineage_citations(lineage, path):
        for relative, _key, value in _walk(lineage):
            if not isinstance(value, str):
                continue
            attribution = relative in (('attribution',), ('rights', 'attribution')) and value == _GDELT_ATTRIBUTION
            rights = relative in (('rights_evidence_url',), ('rights', 'evidence_url')) and value == 'https://gdeltproject.org/about.html'
            archive_path = relative == ('url',) or (len(relative) == 3 and relative[0] in ('sources', 'attempts') and isinstance(relative[1], int) and relative[2] == 'url')
            if attribution or rights or archive_path and _GDELT_ARCHIVE.fullmatch(value):
                allowed.add(_pointer((*path, *relative)))

    def inspect(value, path):
        if not isinstance(value, dict):
            return
        protocol, version = value.get('feature_protocol'), value.get('feature_version')
        if isinstance(protocol, str) and protocol in _GDELT_VERSIONS and isinstance(value.get('provenance'), dict):
            proof = value['provenance']
            if isinstance(proof.get('lineage'), dict):
                lineage_citations(proof['lineage'], (*path, 'provenance', 'lineage'))
        if isinstance(version, str) and version in _GDELT_VERSIONS and isinstance(value.get('lineage'), dict):
            lineage_citations(value['lineage'], (*path, 'lineage'))
        document, encoded = value.get('document'), value.get('canonical_document_json')
        if isinstance(document, dict) and isinstance(document.get('feature_version'), str) and document['feature_version'] in _GDELT_VERSIONS and isinstance(encoded, str):
            try:
                if json.loads(encoded) == document and not response_leaks(document):
                    allowed.add(_pointer((*path, 'canonical_document_json')))
            except (ValueError, TypeError):
                pass

    inspect(response, ())
    for path, _key, value in _walk(response):
        inspect(value, path)
    return allowed
