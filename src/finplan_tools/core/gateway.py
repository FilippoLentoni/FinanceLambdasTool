"""AgentCore Gateway invocation adapter: the ONLY module that knows the Gateway event shape (D3).

LT-OQ-1 is open: how AgentCore Gateway presents a tool call to a Lambda target (event and context
shape, end-user identity propagation) is **not verified**. This adapter encodes the documented
shape as currently understood and nothing else depends on it, so the answer changes this module
only:

* the Lambda ``event`` is the tool's argument object;
* ``context.client_context.custom`` carries Gateway metadata, including
  ``bedrockAgentCoreToolName`` (``<target>___<tool>``).

No end-user identity is taken from the Gateway until LT-OQ-1 closes: every Gateway caller in an
environment shares the identity ``gateway:<env>`` (design Risks).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

__all__ = ["GatewayCall", "parse_gateway_call", "TOOL_NAME_KEY", "TOOL_NAME_SEPARATOR"]

TOOL_NAME_KEY = "bedrockAgentCoreToolName"
TOOL_NAME_SEPARATOR = "___"


@dataclass(frozen=True)
class GatewayCall:
    tool_name: str | None
    arguments: Any
    message_id: str | None = None


def _custom(context: Any) -> Mapping[str, Any] | None:
    cc = getattr(context, "client_context", None)
    if cc is None and isinstance(context, Mapping):
        cc = context.get("client_context")
    if cc is None:
        return None
    custom = getattr(cc, "custom", None)
    if custom is None and isinstance(cc, Mapping):
        custom = cc.get("custom")
    return custom if isinstance(custom, Mapping) else None


def parse_gateway_call(event: Any, context: Any) -> GatewayCall | None:
    """A :class:`GatewayCall` when the invocation carries Gateway context, else None."""
    custom = _custom(context)
    if not custom or not isinstance(custom.get(TOOL_NAME_KEY), str):
        return None
    full = str(custom[TOOL_NAME_KEY])
    tool = full.split(TOOL_NAME_SEPARATOR)[-1] if full else None
    msg = custom.get("bedrockAgentCoreMcpMessageId")
    return GatewayCall(tool_name=tool or None, arguments=event, message_id=str(msg) if msg is not None else None)
