"""``production_strategy`` (change add-approval-and-strategy-tools; spec production-strategy-tool).

One tool with ``action`` ``get`` | ``set`` | ``clear`` (design T1) over FinanceModel's
production-strategy selection operations (``GET``/``PUT /v1/production-strategy``, contracts 1.1.0):

* ``get`` returns the environment's current selection document, or ``strategy: null`` when none is
  selected (the daily recommendation job is then a no-op).
* ``set`` (``strategy_id``) and ``clear`` are confirmed user actions (T2, T3). Before any FinanceModel
  call they require ``confirmed_by_user: true`` (``PRECONDITION_FAILED`` ``confirmation_required``),
  an ``idempotency_key`` (``VALIDATION_FAILED``) and a caller in group ``plan_publisher``
  (``FORBIDDEN``). The confirmation is forwarded to FinanceModel, which enforces it again, with the
  derived idempotency key and the on-behalf-of caller block.

``confirmed_by_user`` is a tool-only field: the pinned 1.1.0 request schema does not declare it
(``additionalProperties: false``; CONTRACT GAP for a later contract minor), so the pipeline removes it
before schema validation and the tool checks it after validation, before any FinanceModel call.
FinanceModel decides validity: its errors (for example ``VALIDATION_FAILED`` with
``no_evaluation_evidence``) pass through unchanged. The tool never writes SSM, never submits jobs,
never publishes plans and never records executions. Until FinanceModel's 1.1.0 release is in the
environment it answers ``DEPENDENCY_UNAVAILABLE`` (``operation_not_released``).
"""

from __future__ import annotations

from typing import Any, Mapping

from ..core.contracts import validate_document
from ..core.errors import FORBIDDEN, PRECONDITION_FAILED, ToolError
from ..core.registry import register_tool
from ._common import producer_doc

RESPONSE = "tools/production-strategy-response"
WRITE_ACTIONS = ("set", "clear")
PUBLISHER_GROUP = "plan_publisher"
CONFIRMATION_FIELD = "confirmed_by_user"


def _writes(request: Mapping[str, Any]) -> bool:
    return request.get("action") in WRITE_ACTIONS


def _authorize(invocation: Any, request: Mapping[str, Any], tool_fields: Mapping[str, Any]) -> None:
    """Group, then confirmation, for set/clear; runs before any FinanceModel call."""
    if CONFIRMATION_FIELD in tool_fields and not isinstance(tool_fields[CONFIRMATION_FIELD], bool):
        raise ToolError.validation("confirmed_by_user must be a boolean", pointer=f"/{CONFIRMATION_FIELD}")
    if not _writes(request):
        return
    if PUBLISHER_GROUP not in invocation.groups:
        raise ToolError(FORBIDDEN, f"changing the production strategy requires a caller in group {PUBLISHER_GROUP}", reason="group_required", required_group=PUBLISHER_GROUP)
    if tool_fields.get(CONFIRMATION_FIELD) is not True:
        raise ToolError(
            PRECONDITION_FAILED,
            "changing the production strategy needs the user's explicit confirmation (confirmed_by_user: true)",
            reason="confirmation_required",
            action=request.get("action"),
        )


def _strategy(body: Mapping[str, Any], environment: str) -> dict[str, Any] | None:
    """The selection document of a FinanceModel answer (``{"strategy": doc|null, ...}`` or the
    document itself); an empty answer or a document without ``strategy_id`` means none."""
    doc: Any = body.get("strategy") if "strategy" in body else body
    if doc is None or (isinstance(doc, Mapping) and not doc.get("strategy_id")):
        return None
    doc = producer_doc(doc, "production strategy")
    res = validate_document(doc, "production-strategy")
    if not res.valid:
        raise ToolError.internal("FinanceModel returned a non-conformant production-strategy document")
    if doc.get("environment") != environment:
        raise ToolError.internal("FinanceModel returned the production strategy of another environment")
    return doc


@register_tool(
    "production_strategy",
    description=(
        "Read or change the production strategy that this environment's daily recommendation job runs. "
        "action=get returns the current selection or null (null means daily recommendations are off). "
        "action=set (strategy_id) and action=clear change it: they require an idempotency_key, the "
        "user's explicit confirmation in the conversation (confirmed_by_user: true) and a caller in "
        "group plan_publisher. FinanceModel validates the strategy against its registry; its "
        "rejections (for example no_evaluation_evidence) are returned unchanged. Never trades."
    ),
    writes=_writes,
    authorize=_authorize,
    tool_only_fields=(CONFIRMATION_FIELD,),
)
def production_strategy(ctx: Any, request: dict[str, Any]) -> dict[str, Any]:
    action = request["action"]
    if action == "get":
        body = producer_doc(ctx.jobs.get_production_strategy(ctx.meta), "production strategy")
        changed = False
    else:
        down = ctx.downstream_body(request, extra={CONFIRMATION_FIELD: True})
        body = producer_doc(ctx.jobs.put_production_strategy(down, ctx.meta), "production strategy")
        changed = body.get("changed") if isinstance(body.get("changed"), bool) else True
    strategy = _strategy(body, ctx.environment)
    if action == "set" and (strategy is None or strategy.get("strategy_id") != request["strategy_id"]):
        raise ToolError.internal("FinanceModel selected another strategy than requested")
    if action == "clear" and strategy is not None:
        raise ToolError.internal("FinanceModel still reports a production strategy after clear")
    doc: dict[str, Any] = {"environment": ctx.environment, "action": action, "strategy": strategy, "changed": changed}
    if request.get("synthetic") is True:
        doc["synthetic"] = True
    return doc
