"""Compact, size-bounded responses (D7, TRH-10).

* :func:`response_size` is the byte size of the compact JSON the handler returns.
* :func:`bound_list` cuts a list field until the response fits ``response_max_bytes`` and sets
  ``truncated`` and a continuation token.
* :func:`wrap_token` / :func:`unwrap_token` make opaque continuation tokens that wrap the producer's
  token (or an offset into a producer page). A token is never a storage pointer, and it is bound to
  the tool and environment that issued it.
* :func:`summarize_weights` keeps the top N weights plus an ``other`` bucket.
"""

from __future__ import annotations

import base64
import json
import re
from typing import Any, Mapping

from .errors import ToolError

__all__ = ["response_size", "bound_list", "wrap_token", "unwrap_token", "summarize_weights", "TOKEN_PATTERN"]

TOKEN_PATTERN = re.compile(r"^[A-Za-z0-9_=-]{1,2048}\Z")
_TOKEN_VERSION = 1


def _dumps(doc: Any) -> str:
    return json.dumps(doc, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def response_size(doc: Any) -> int:
    return len(_dumps(doc).encode("utf-8"))


def wrap_token(*, tool: str, environment: str, producer_token: str | None = None, offset: int | None = None) -> str:
    """Opaque token for the next page (base64url JSON; matches the contract ``next_token`` pattern)."""
    payload: dict[str, Any] = {"v": _TOKEN_VERSION, "t": tool, "e": environment}
    if producer_token is not None:
        payload["p"] = producer_token
    if offset is not None:
        payload["o"] = int(offset)
    token = base64.urlsafe_b64encode(_dumps(payload).encode("utf-8")).decode("ascii")
    if not TOKEN_PATTERN.match(token):
        raise ToolError.internal("continuation token too long")
    return token


def unwrap_token(token: str, *, tool: str, environment: str) -> dict[str, Any]:
    """Decode a token issued by :func:`wrap_token` for this tool and environment."""
    try:
        if not isinstance(token, str) or not TOKEN_PATTERN.match(token):
            raise ValueError
        payload = json.loads(base64.urlsafe_b64decode(token.encode("ascii")))
        if not isinstance(payload, dict) or payload.get("v") != _TOKEN_VERSION:
            raise ValueError
    except (ValueError, TypeError, json.JSONDecodeError):
        raise ToolError.validation("next_token is not a token issued by this tool", pointer="/next_token") from None
    if payload.get("t") != tool or payload.get("e") != environment:
        raise ToolError.validation("next_token was issued by another tool or environment", pointer="/next_token")
    out: dict[str, Any] = {}
    if isinstance(payload.get("p"), str):
        out["producer_token"] = payload["p"]
    if isinstance(payload.get("o"), int) and payload["o"] >= 0:
        out["offset"] = payload["o"]
    return out


def bound_list(
    doc: dict[str, Any],
    list_key: str,
    max_bytes: int,
    *,
    tool: str,
    environment: str,
    start_offset: int = 0,
    producer_token: str | None = None,
    token_key: str = "next_token",
    truncated_key: str | None = "truncated",
) -> dict[str, Any]:
    """Return ``doc`` with ``doc[list_key]`` cut so the whole response fits ``max_bytes``.

    When items are dropped, ``truncated`` (if ``truncated_key``) is true and ``token_key`` holds a
    token resuming at the first dropped item (offset into the current producer page, plus the
    producer token of that page so the next call re-reads the same page).
    """
    if response_size(doc) <= max_bytes:
        return doc
    items = list(doc.get(list_key) or [])
    out = dict(doc)
    lo, hi = 0, len(items)
    best: dict[str, Any] | None = None
    while lo <= hi:  # largest prefix that fits
        mid = (lo + hi) // 2
        cand = dict(out)
        cand[list_key] = items[:mid]
        if mid < len(items):
            cand[token_key] = wrap_token(tool=tool, environment=environment, producer_token=producer_token, offset=start_offset + mid)
            if truncated_key:
                cand[truncated_key] = True
        if response_size(cand) <= max_bytes:
            best, lo = cand, mid + 1
        else:
            hi = mid - 1
    if best is None or not best[list_key] and items:
        raise ToolError.internal("the response cannot be bounded to the configured size", reason="response_too_large")
    return best


def summarize_weights(weights: Mapping[str, float], top_n: int) -> dict[str, Any]:
    """Top ``top_n`` weights (by absolute value, ties by key) plus an ``other`` bucket."""
    ordered = sorted(weights.items(), key=lambda kv: (-abs(float(kv[1])), kv[0]))
    top = [{"key": k, "weight": float(v)} for k, v in ordered[:top_n]]
    rest = ordered[top_n:]
    return {
        "top": top,
        "other": {"count": len(rest), "weight": round(sum(float(v) for _, v in rest), 12)},
        "total_count": len(ordered),
    }
