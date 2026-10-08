"""Deployment settings and the ``tool-limits`` configuration (D5, D7, D9).

The Lambda knows its own environment, release and tool from its deployment configuration
(environment variables set by the stack: ``FINPLAN_ENV``, ``FINPLAN_RELEASE_ID``,
``FINPLAN_TOOL_NAME``). No endpoint, ARN or account identifier is ever configured here: producer
references are resolved at run time from same-environment SSM (:mod:`.references`).

``tool-limits`` lives at ``/finplan/<env>/financelambdastool/config/tool-limits`` (written by the
bootstrap from ``config/tool-limits.default.json`` unless present). The defaults below are the
same document; a unit test keeps the two identical.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from typing import Any, Mapping

__all__ = [
    "ENVIRONMENTS",
    "REPO",
    "DEFAULT_TOOL_LIMITS",
    "Settings",
    "ToolLimits",
    "tool_limits_problems",
    "limit_bound_problems",
]

ENVIRONMENTS = ("beta", "gamma", "prod")
REPO = "financelambdastool"
REGION = "us-east-2"
_RELEASE_ID = re.compile(r"^rel_[0-7][0-9A-HJKMNP-TV-Z]{25}\Z")
_TOOL = re.compile(r"^[a-z][a-z0-9_]{1,63}\Z")
BUDGET_CATEGORIES = ("platform_infra", "cpu_research", "bedrock_explanations", "gpu", "reserve")

DEFAULT_TOOL_LIMITS: dict[str, Any] = {
    "response_max_bytes": 65536,
    "per_call_max_fraction": 0.2,
    "max_estimated_usd_per_call": {
        "platform_infra": 0,
        "cpu_research": 1.0,
        "bedrock_explanations": 0,
        "gpu": 5.0,
        "reserve": 0,
    },
    "page_size_default": 20,
    "page_size_max": 100,
    "summary_top_n": 10,
    "reference_ttl_seconds": 300,
    "timeouts_seconds": {"read": 15, "write": 30, "refresh_market_data": 60},
}


def tool_limits_problems(doc: Any) -> list[str]:
    """Shape problems of a ``tool-limits`` document (empty when valid)."""
    if not isinstance(doc, Mapping):
        return ["tool-limits must be a JSON object"]
    out: list[str] = []

    def num(key: str, *, lo: float = 0, integer: bool = False) -> None:
        v = doc.get(key)
        if isinstance(v, bool) or not isinstance(v, (int, float)) or (integer and not isinstance(v, int)) or v < lo:
            out.append(f"{key} must be a {'integer' if integer else 'number'} >= {lo:g}")

    num("response_max_bytes", lo=1024, integer=True)
    num("page_size_default", lo=1, integer=True)
    num("page_size_max", lo=1, integer=True)
    num("summary_top_n", lo=1, integer=True)
    num("reference_ttl_seconds", lo=0, integer=True)
    frac = doc.get("per_call_max_fraction")
    if isinstance(frac, bool) or not isinstance(frac, (int, float)) or not 0 < frac <= 1:
        out.append("per_call_max_fraction must be a number in (0, 1]")
    limits = doc.get("max_estimated_usd_per_call")
    if not isinstance(limits, Mapping) or not limits:
        out.append("max_estimated_usd_per_call must be a non-empty map of budget category -> USD")
    else:
        for cat, v in limits.items():
            if cat not in BUDGET_CATEGORIES:
                out.append(f"max_estimated_usd_per_call names unregistered budget category {cat!r}")
            if isinstance(v, bool) or not isinstance(v, (int, float)) or v < 0:
                out.append(f"max_estimated_usd_per_call.{cat} must be a number >= 0")
    if isinstance(doc.get("page_size_default"), int) and isinstance(doc.get("page_size_max"), int) and doc["page_size_default"] > doc["page_size_max"]:
        out.append("page_size_default must not exceed page_size_max")
    t = doc.get("timeouts_seconds")
    if not isinstance(t, Mapping) or not all(isinstance(v, int) and not isinstance(v, bool) and 1 <= v <= 900 for v in t.values()):
        out.append("timeouts_seconds must map names to integer seconds in [1, 900]")
    return out


def limit_bound_problems(limits_doc: Mapping[str, Any], allocation: Mapping[str, Any]) -> list[str]:
    """Pre-deploy check (D5, task 6.2a): each per-call limit <= fraction x category allocation."""
    out: list[str] = []
    frac = float(limits_doc.get("per_call_max_fraction", DEFAULT_TOOL_LIMITS["per_call_max_fraction"]))
    for cat, v in (limits_doc.get("max_estimated_usd_per_call") or {}).items():
        if cat not in allocation:
            out.append(f"tool-limits names budget category {cat!r}, which is not in the budget allocation")
            continue
        bound = round(frac * float(allocation[cat]), 6)
        if float(v) > bound + 1e-9:
            out.append(f"per-call limit for {cat} is USD {float(v):.2f}, above the bound USD {bound:.2f} ({frac:g} x allocation {float(allocation[cat]):g})")
    return out


@dataclass(frozen=True)
class ToolLimits:
    response_max_bytes: int
    per_call_max_fraction: float
    max_estimated_usd_per_call: dict[str, float]
    page_size_default: int
    page_size_max: int
    summary_top_n: int
    reference_ttl_seconds: int
    timeouts_seconds: dict[str, int]
    raw: dict[str, Any] = field(default_factory=dict, compare=False, repr=False)

    @classmethod
    def from_document(cls, doc: Mapping[str, Any] | None) -> "ToolLimits":
        merged = {**DEFAULT_TOOL_LIMITS, **dict(doc or {})}
        problems = tool_limits_problems(merged)
        if problems:
            raise ValueError("invalid tool-limits: " + "; ".join(problems))
        return cls(
            response_max_bytes=int(merged["response_max_bytes"]),
            per_call_max_fraction=float(merged["per_call_max_fraction"]),
            max_estimated_usd_per_call={k: float(v) for k, v in merged["max_estimated_usd_per_call"].items()},
            page_size_default=int(merged["page_size_default"]),
            page_size_max=int(merged["page_size_max"]),
            summary_top_n=int(merged["summary_top_n"]),
            reference_ttl_seconds=int(merged["reference_ttl_seconds"]),
            timeouts_seconds={k: int(v) for k, v in merged["timeouts_seconds"].items()},
            raw=merged,
        )

    @classmethod
    def from_json(cls, text: str | None) -> "ToolLimits":
        return cls.from_document(json.loads(text) if text else None)

    def per_call_limit(self, category: str) -> float | None:
        """Per-call USD limit of a budget category; None when the category is not configured."""
        return self.max_estimated_usd_per_call.get(category)


@dataclass(frozen=True)
class Settings:
    """What a tool Lambda knows about itself from its deployment configuration."""

    environment: str
    release_id: str | None
    tool_name: str | None
    region: str = REGION

    def __post_init__(self) -> None:
        if self.environment not in ENVIRONMENTS:
            raise ValueError(f"environment must be one of {ENVIRONMENTS}")
        if self.release_id is not None and not _RELEASE_ID.match(self.release_id):
            raise ValueError("release_id must be 'rel_' + ULID")
        if self.tool_name is not None and not _TOOL.match(self.tool_name):
            raise ValueError("tool_name must be a snake_case tool name")

    @classmethod
    def from_environ(cls, environ: Mapping[str, str] | None = None) -> "Settings":
        env = os.environ if environ is None else environ
        return cls(
            environment=env.get("FINPLAN_ENV", ""),
            release_id=env.get("FINPLAN_RELEASE_ID") or None,
            tool_name=env.get("FINPLAN_TOOL_NAME") or None,
            region=env.get("AWS_REGION") or env.get("AWS_DEFAULT_REGION") or REGION,
        )
