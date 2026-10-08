"""Lambda entry point shared by every tool function (one artifact, one function per tool; D1).

CDK wiring (INFRA): every function uses the handler ``finplan_tools.handler.handler`` with the
environment variables ``FINPLAN_ENV`` (``beta|gamma|prod``), ``FINPLAN_TOOL_NAME`` (a
:data:`finplan_tools.core.registry.CATALOG` name) and ``FINPLAN_RELEASE_ID`` (``rel_<ULID>``).
Nothing else is configured: producer endpoints, manifests and ``tool-limits`` are resolved at run
time from same-environment SSM.

The handler returns the tool response document on success and the contract error envelope on
failure (both as the Lambda result payload; the function itself never raises).
"""

from __future__ import annotations

import logging
from typing import Any

from .core import registry
from .core.errors import ToolError
from .core.pipeline import Runtime, execute, new_correlation_id
from .tools import load_all

__all__ = ["handler", "invoke"]

logging.getLogger().setLevel(logging.INFO)
_RUNTIME: Runtime | None = None


def _runtime() -> Runtime:
    global _RUNTIME
    if _RUNTIME is None:
        _RUNTIME = Runtime.from_environment()
    return _RUNTIME


def invoke(tool_name: str, event: Any, context: Any, runtime: Runtime) -> dict[str, Any]:
    """Run ``tool_name`` through the shared pipeline with an explicit runtime (tests, local runs)."""
    load_all()
    spec = registry.get_tool(tool_name)
    if spec is None:
        return ToolError.internal("this tool is not implemented in this release", reason="tool_not_registered").to_envelope(new_correlation_id())
    return execute(spec, event, context, runtime)


def handler(event: Any, context: Any) -> dict[str, Any]:
    try:
        rt = _runtime()
    except Exception:  # noqa: BLE001 - misconfigured function: logged, never a trace to the caller
        cid = new_correlation_id()
        logging.getLogger("finplan_tools.handler").exception("runtime construction failed (correlation_id=%s)", cid)
        return ToolError.internal("the function is misconfigured", reason="runtime_unavailable").to_envelope(cid)
    if not rt.settings.tool_name:
        return ToolError.internal("the function has no tool configured", reason="tool_not_configured").to_envelope(new_correlation_id())
    return invoke(rt.settings.tool_name, event, context, rt)
