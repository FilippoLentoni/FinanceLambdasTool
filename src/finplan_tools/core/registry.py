"""Tool registry: the interface every tool module implements (design D1).

A tool is a :class:`ToolSpec` plus a ``run(ctx, request)`` function. The shared pipeline
(:mod:`finplan_tools.core.pipeline`) does everything else: identity, contract major, storage-input
rejection, schema validation, environment check, dependency gating, error mapping, response
bounding, output validation and the audit record. ``run`` receives a request that already validated
against the pinned input schema and returns the response document (it is validated against the
pinned output schema before it is sent).

Writing a tool (in ``finplan_tools/tools/<name>.py``; every module there is imported by
``finplan_tools.tools.load_all``)::

    from finplan_tools.core.registry import register_tool

    @register_tool("get_plan_version", description="Read one immutable plan version ...")
    def get_plan_version(ctx, request):
        doc = ctx.platform.get_plan_version(request["plan_version_id"], ctx.meta)
        return {"plan_version": doc["plan_version"], ...}

The static :data:`CATALOG` fixes the tool inventory, kinds and role classes of D1, so the CDK app can
build one function per tool from it, and :func:`register_tool` refuses a tool that is not in the
catalog, names a denied capability (execute/trade/order/payment/wallet/execution) or disagrees with
the catalog's kind or role class.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable, Iterable

from .contracts import schema_id, store

if TYPE_CHECKING:  # pragma: no cover
    from .pipeline import ToolContext

__all__ = [
    "ROLE_CLASSES",
    "PRODUCER_PLATFORM",
    "PRODUCER_MODEL",
    "CatalogEntry",
    "CATALOG",
    "ToolSpec",
    "register_tool",
    "get_tool",
    "registered_tools",
    "catalog_names",
    "inventory_problems",
    "DENIED_WORDS",
]

ROLE_CLASSES = ("reader", "submitter", "plan-writer")
PRODUCER_PLATFORM = "financialplanning"
PRODUCER_MODEL = "financemodel"
#: Tool inventory deny-list (D6, PLN-08): no execution, trading, order, payment or wallet tool.
DENIED_WORDS = ("execute", "execution", "trade", "trading", "order", "payment", "wallet")
_NAME = re.compile(r"^[a-z][a-z0-9_]{1,63}\Z")


@dataclass(frozen=True)
class CatalogEntry:
    name: str
    state_changing: bool
    role_class: str
    producers: tuple[str, ...]
    timeout_key: str
    #: Lowest producer contract release (``contract_version`` of its release manifest) that serves
    #: the operations this tool calls; an older producer release -> ``DEPENDENCY_UNAVAILABLE``.
    min_producer_contract: str | None = None

    @property
    def input_schema(self) -> str:
        return _schema_name(self.name, "request")

    @property
    def output_schema(self) -> str:
        return _schema_name(self.name, "response")

    @property
    def lambda_ref_name(self) -> str:
        """SSM name segment of the tool's Lambda reference: ``<tool-with-dashes>-arn``."""
        return f"{self.name.replace('_', '-')}-arn"

    @property
    def prod_direct_test(self) -> bool:
        """Whether the prod direct-test principal and smoke role may invoke it (read-only tools only)."""
        return not self.state_changing


def _schema_name(tool: str, kind: str) -> str:
    return f"tools/{tool.replace('_', '-')}-{kind}"


#: D1: tools, kind, role class, producers each tool depends on, timeout class.
CATALOG: dict[str, CatalogEntry] = {
    e.name: e
    for e in (
        CatalogEntry("describe_capabilities", False, "reader", (), "read"),
        CatalogEntry("query_market_data", False, "reader", (PRODUCER_PLATFORM,), "read"),
        CatalogEntry("get_plan", False, "reader", (PRODUCER_PLATFORM,), "read"),
        CatalogEntry("get_plan_version", False, "reader", (PRODUCER_PLATFORM,), "read"),
        CatalogEntry("list_plan_versions", False, "reader", (PRODUCER_PLATFORM,), "read"),
        CatalogEntry("get_job_status", False, "reader", (PRODUCER_MODEL,), "read"),
        CatalogEntry("get_experiment_result", False, "reader", (PRODUCER_MODEL,), "read"),
        CatalogEntry("refresh_market_data", True, "submitter", (PRODUCER_PLATFORM,), "refresh_market_data"),
        CatalogEntry("submit_experiment", True, "submitter", (PRODUCER_PLATFORM, PRODUCER_MODEL), "write"),
        CatalogEntry("create_override_version", True, "plan-writer", (PRODUCER_PLATFORM,), "write"),
        CatalogEntry("validate_plan_version", True, "plan-writer", (PRODUCER_PLATFORM,), "write"),
        CatalogEntry("publish_plan_version", True, "plan-writer", (PRODUCER_PLATFORM,), "write"),
        # contracts 1.1.0 (add-approval-and-strategy-tools): get/set/clear of FinanceModel's
        # production strategy; FinanceModel serves the selection operations from its 1.1.0 release.
        CatalogEntry("production_strategy", True, "plan-writer", (PRODUCER_MODEL,), "write", min_producer_contract="1.1.0"),
    )
}

RunFn = Callable[["ToolContext", dict[str, Any]], dict[str, Any]]


@dataclass(frozen=True)
class ToolSpec:
    """A registered tool: its catalog entry, description and implementation."""

    entry: CatalogEntry
    description: str
    run: RunFn
    #: Name of the list field the pipeline may cut to fit the byte limit (sets ``truncated`` /
    #: ``next_token`` per :func:`finplan_tools.core.bounds.bound_list`), or None.
    list_key: str | None = None
    #: Field set true when ``list_key`` is cut (None when the output schema has no such field).
    truncated_key: str | None = "truncated"
    #: JSON-pointer prefixes where the tool returns platform-issued download grants (none in 1.0.0).
    grant_pointers: tuple[str, ...] = ()
    #: Gate on producer release manifests before ``run`` (``describe_capabilities`` sets False).
    gate_dependencies: bool = True
    #: Optional check of the raw request run after the storage-input check and before schema
    #: validation (raise :class:`~finplan_tools.core.errors.ToolError`); no producer call allowed.
    pre_validate: Callable[[dict[str, Any]], None] | None = None
    #: Whether THIS request changes state (default: the catalog kind). A tool with read and write
    #: actions (``production_strategy``) requires an ``idempotency_key`` only for its writes.
    writes: Callable[[dict[str, Any]], bool] | None = None
    #: Optional caller authorization ``(invocation, request, tool_fields)`` run after schema
    #: validation and the environment check, before dependency gating and any producer call.
    authorize: Callable[[Any, dict[str, Any], dict[str, Any]], None] | None = None
    #: Request fields the tool reads that the pinned request schema does not declare: the pipeline
    #: removes them before schema validation and hands them to ``authorize`` and ``ctx.tool_fields``.
    tool_only_fields: tuple[str, ...] = ()
    extra: dict[str, Any] = field(default_factory=dict)

    def request_writes(self, request: dict[str, Any]) -> bool:
        return self.writes(request) if self.writes is not None else self.entry.state_changing

    @property
    def name(self) -> str:
        return self.entry.name

    @property
    def state_changing(self) -> bool:
        return self.entry.state_changing

    @property
    def role_class(self) -> str:
        return self.entry.role_class

    @property
    def producers(self) -> tuple[str, ...]:
        return self.entry.producers

    @property
    def input_schema(self) -> str:
        return self.entry.input_schema

    @property
    def output_schema(self) -> str:
        return self.entry.output_schema

    @property
    def input_schema_id(self) -> str:
        return schema_id(self.input_schema)

    @property
    def output_schema_id(self) -> str:
        return schema_id(self.output_schema)


_REGISTRY: dict[str, ToolSpec] = {}


def inventory_problems(names: Iterable[str]) -> list[str]:
    """Deny-list and naming problems of a tool inventory (build check, PLN-08)."""
    out = []
    for n in names:
        if not _NAME.match(n):
            out.append(f"tool name {n!r} is not snake_case")
        hit = [w for w in DENIED_WORDS if w in n]
        if hit:
            out.append(f"tool {n!r} names a denied capability ({', '.join(hit)})")
    return out


def register_tool(
    name: str,
    *,
    description: str,
    list_key: str | None = None,
    truncated_key: str | None = "truncated",
    grant_pointers: tuple[str, ...] = (),
    gate_dependencies: bool = True,
    pre_validate: Callable[[dict[str, Any]], None] | None = None,
    writes: Callable[[dict[str, Any]], bool] | None = None,
    authorize: Callable[[Any, dict[str, Any], dict[str, Any]], None] | None = None,
    tool_only_fields: tuple[str, ...] = (),
    replace: bool = False,
) -> Callable[[RunFn], RunFn]:
    """Decorator registering ``run`` as the implementation of catalog tool ``name``."""
    problems = inventory_problems([name])
    if problems:
        raise ValueError("; ".join(problems))
    entry = CATALOG.get(name)
    if entry is None:
        raise ValueError(f"tool {name!r} is not in the D1 catalog")
    for schema in (entry.input_schema, entry.output_schema):
        if schema not in store():
            raise ValueError(f"tool {name!r}: schema {schema} is not in the pinned contract package")
    if not 1 <= len(description) <= 1000:
        raise ValueError("description must be 1-1000 characters (tool-catalog schema)")

    def deco(fn: RunFn) -> RunFn:
        if name in _REGISTRY and not replace and _REGISTRY[name].run is not fn:
            raise ValueError(f"tool {name!r} is already registered")
        _REGISTRY[name] = ToolSpec(entry, description, fn, list_key, truncated_key, tuple(grant_pointers), gate_dependencies, pre_validate, writes, authorize, tuple(tool_only_fields))
        return fn

    return deco


def get_tool(name: str) -> ToolSpec | None:
    return _REGISTRY.get(name)


def registered_tools() -> list[ToolSpec]:
    return [_REGISTRY[n] for n in sorted(_REGISTRY)]


def catalog_names() -> list[str]:
    return sorted(CATALOG)


def _unregister(name: str) -> None:
    """Test helper: drop a registration."""
    _REGISTRY.pop(name, None)
