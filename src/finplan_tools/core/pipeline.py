"""The shared request pipeline every tool runs (design D2; spec tool-request-handling).

:func:`execute` is the whole adapter around a tool's ``run``:

1. resolve the invocation source to a caller identity (``UNAUTHORIZED`` otherwise);
2. check the declared contract major (``UNSUPPORTED_CONTRACT_VERSION`` with the served majors);
3. reject caller-chosen storage locations before any other work (``VALIDATION_FAILED``);
4. validate the request with the pinned contract validators (``VALIDATION_FAILED`` or
   ``INVALID_IDENTIFIER`` with the failing JSON pointer); write tools need ``idempotency_key``;
5. check the declared environment against the Lambda's own (``FORBIDDEN``); in prod the direct-test
   and pipeline smoke sources may call read-only tools only (``FORBIDDEN``);
6. gate on the producers' release manifests in this environment (``DEPENDENCY_UNAVAILABLE`` /
   ``UNSUPPORTED_CONTRACT_VERSION``), then run the tool (its own preconditions and producer calls);
7. map every failure to the contract error envelope (producer codes and ``retryable`` kept,
   anything unexpected ``INTERNAL`` with the trace only in the log under the correlation ID);
8. bound the response to ``tool-limits.response_max_bytes``;
9. validate the response against the pinned output schema and scan it for storage locations,
   ARNs, account IDs and endpoints: a non-conformant response is never sent (``INTERNAL``);
10. write exactly one audit record.

Success returns the tool's response document; failure returns the error envelope. No producer is
called before step 6, which every negative test asserts through the mocks' call counters.
"""

from __future__ import annotations

import os

import logging
import re
import time
import uuid
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import Any, Callable, Mapping

from . import audit
from .artifacts import response_leaks, storage_input_problems
from .bounds import bound_list, response_size, unwrap_token, wrap_token
from .compat import check_producer_release
from .config import Settings, ToolLimits
from .contracts import contract_version, parse_major, served_majors, validate_document
from .errors import FORBIDDEN, INTERNAL, ToolError, from_validation
from .identity import INVOCATION_KEY, Invocation, caller_block, resolve_invocation
from .idempotency import derived_key, downstream_body
from .references import MODEL_MANIFEST, PLATFORM_MANIFEST, ReferenceResolver
from .registry import PRODUCER_MODEL, PRODUCER_PLATFORM, ToolSpec
from .transport import CallMeta

__all__ = ["Runtime", "ToolContext", "execute", "new_correlation_id"]

log = logging.getLogger("finplan_tools.pipeline")
_CID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{7,127}\Z")


def new_correlation_id() -> str:
    return "lt-" + uuid.uuid4().hex


@dataclass
class Runtime:
    """Process-wide dependencies of a tool Lambda (built once per container).

    ``platform`` / ``jobs`` are factories ``(timeout_seconds) -> client`` so each invocation gets
    the tool's configured timeout; ``references`` resolves same-environment SSM references.
    Offline tests build a Runtime around the mocks (``finplan_tools_testing.runtime``).
    """

    settings: Settings
    references: ReferenceResolver | None
    platform: Callable[[float], Any]
    jobs: Callable[[float], Any]
    clock: Callable[[], datetime] = field(default=lambda: datetime.now(timezone.utc))
    limits_override: ToolLimits | None = None
    gateway_research_authorizer: Any = None
    durable_activity_receipts: bool = False

    def limits(self) -> ToolLimits:
        if self.limits_override is not None:
            return self.limits_override
        doc = self.references.tool_limits_document() if self.references is not None else None
        return ToolLimits.from_document(doc)

    def manifest(self, producer: str) -> Any:
        if self.references is None:
            return None
        key = {PRODUCER_PLATFORM: PLATFORM_MANIFEST, PRODUCER_MODEL: MODEL_MANIFEST}[producer]
        return self.references.get_json(*key)

    @classmethod
    def from_environment(cls, environ: Mapping[str, str] | None = None) -> "Runtime":
        """Deployed wiring: regional SSM, SigV4 transports resolved from same-environment SSM."""
        from ..backends.jobs import JobClient
        from ..backends.platform import PlatformClient
        from ..backends.sigv4 import SigV4HttpTransport
        from . import aws_clients

        settings = Settings.from_environ(environ)
        sess = aws_clients.session(settings.region)
        refs = ReferenceResolver(aws_clients.ssm_client(settings.region, boto_session=sess), settings.environment)

        def creds() -> Any:
            return aws_clients.credentials(sess)

        plan_t = SigV4HttpTransport(refs.plan_endpoint, region=settings.region, credentials=creds, producer=PRODUCER_PLATFORM)
        ingest_t = SigV4HttpTransport(refs.ingestion_endpoint, region=settings.region, credentials=creds, producer=PRODUCER_PLATFORM)
        job_t = SigV4HttpTransport(refs.job_endpoint, region=settings.region, credentials=creds, producer=PRODUCER_MODEL)
        from botocore.config import Config
        from ..backends.strategy import StrategyLambdaClient
        from ..backends.classical import ClassicalLambdaClient
        values = os.environ if environ is None else environ
        strategy = StrategyLambdaClient(
            sess.client("lambda", region_name=settings.region, config=Config(connect_timeout=3, read_timeout=280, retries={"total_max_attempts": 1})),
            refs, environment=settings.environment, region=settings.region, account=values.get("FINPLAN_ACCOUNT_ID", ""),
        )
        classical = ClassicalLambdaClient(
            sess.client("lambda", region_name=settings.region, config=Config(connect_timeout=3, read_timeout=280, retries={"total_max_attempts": 1})),
            refs, environment=settings.environment, region=settings.region, account=values.get("FINPLAN_ACCOUNT_ID", ""),
        )
        from .gateway_research_auth import GatewayResearchAuthorizer
        return cls(
            settings=settings,
            references=refs,
            platform=lambda timeout: PlatformClient(plan_t, ingest_t, timeout=timeout),
            jobs=lambda timeout: JobClient(job_t, timeout=timeout, strategy_client=strategy, classical_client=classical),
            gateway_research_authorizer=GatewayResearchAuthorizer(refs, settings.region),
            durable_activity_receipts=True,
        )


@dataclass
class ToolContext:
    """What a tool's ``run`` gets: identity, limits, clients and helpers for one invocation."""

    spec: ToolSpec
    runtime: Runtime
    invocation: Invocation
    correlation_id: str
    limits: ToolLimits
    meta: CallMeta
    platform: Any
    jobs: Any
    synthetic: bool = False
    downstream_ids: dict[str, list[str]] = field(default_factory=dict)
    #: The request's tool-only fields (``ToolSpec.tool_only_fields``), removed before validation.
    tool_fields: dict[str, Any] = field(default_factory=dict)

    # ---------------------------------------------------------------- identity
    @property
    def environment(self) -> str:
        return self.runtime.settings.environment

    @property
    def identity(self) -> str:
        return self.invocation.identity

    @property
    def release_id(self) -> str | None:
        return self.runtime.settings.release_id

    def now(self) -> datetime:
        return self.runtime.clock()

    # ---------------------------------------------------------------- helpers
    def derived_key(self, idempotency_key: str) -> str:
        """``lt_`` + SHA-256 of ``identity|env|tool|idempotency_key`` (D4)."""
        return derived_key(self.identity, self.environment, self.spec.name, idempotency_key)

    def downstream_body(self, request: Mapping[str, Any], **kw: Any) -> dict[str, Any]:
        """Deterministic producer body with the derived idempotency key (D4)."""
        return downstream_body(request, identity=self.identity, environment=self.environment, tool=self.spec.name, **kw)

    def wrap_token(self, *, producer_token: str | None = None, offset: int | None = None) -> str:
        return wrap_token(tool=self.spec.name, environment=self.environment, producer_token=producer_token, offset=offset)

    def unwrap_token(self, token: str) -> dict[str, Any]:
        return unwrap_token(token, tool=self.spec.name, environment=self.environment)

    def bound(self, doc: dict[str, Any], list_key: str, *, start_offset: int = 0, producer_token: str | None = None, truncated_key: str | None = "truncated") -> dict[str, Any]:
        return bound_list(doc, list_key, self.limits.response_max_bytes, tool=self.spec.name, environment=self.environment, start_offset=start_offset, producer_token=producer_token, truncated_key=truncated_key)

    def record_ids(self, *docs: Any, **ids: str) -> None:
        """Add downstream identifiers to the audit record (only contract identifier fields kept)."""
        found = audit.collect_ids(*docs, ids)
        for k, vals in found.items():
            lst = self.downstream_ids.setdefault(k, [])
            for v in vals:
                if v not in lst:
                    lst.append(v)

    def manifest(self, producer: str) -> Any:
        return self.runtime.manifest(producer)


def _peek_correlation_id(event: Any) -> str:
    if isinstance(event, Mapping) and isinstance(event.get(INVOCATION_KEY), Mapping):
        hint = event[INVOCATION_KEY].get("correlation_id")
        if isinstance(hint, str) and _CID.match(hint):
            return hint
    return new_correlation_id()


def _check_contract_major(inv: Invocation, request: Mapping[str, Any]) -> None:
    declared = request.get("contract_version", inv.declared_contract_version)
    if inv.declared_contract_version is not None and "contract_version" in request and request["contract_version"] != inv.declared_contract_version:
        raise ToolError.validation("the invocation and the request declare different contract versions", pointer="/contract_version")
    if declared is None:
        return  # optional in every tool request schema: the release's pinned major applies
    major = parse_major(declared)
    if major is None:
        raise ToolError.validation("contract_version must be a semantic version", pointer="/contract_version")
    if major not in served_majors():
        raise ToolError.unsupported_contract_version(served_majors(), declared=major)


def _envelope(err: ToolError, cid: str, synthetic: bool) -> dict[str, Any]:
    env = err.to_envelope(cid, synthetic=synthetic)
    res = validate_document(env, "error")
    if not res.valid:  # never send a non-conformant envelope
        log.error("error envelope failed validation (correlation_id=%s): %s", cid, [i.message for i in res.issues][:5])
        env = ToolError(INTERNAL, "unexpected failure").to_envelope(cid, synthetic=synthetic)
    return env


def execute(spec: ToolSpec, event: Any, context: Any, runtime: Runtime) -> dict[str, Any]:
    """Run ``spec`` for one Lambda invocation; returns the response or the error envelope."""
    started = time.monotonic()
    cid = _peek_correlation_id(event)
    inv: Invocation | None = None
    ctx: ToolContext | None = None
    synthetic = False
    outcome, retryable = "OK", None
    request: Any = {}
    response: dict[str, Any] | None = None
    try:
        # 1. invocation source -> identity
        inv = resolve_invocation(event, context, environment=runtime.settings.environment)
        if inv.correlation_hint and _CID.match(inv.correlation_hint):
            cid = inv.correlation_hint
        if inv.tool_name is not None and inv.tool_name != spec.name:
            raise ToolError.validation("the invocation names another tool than this function serves", pointer="/tool")
        request = inv.arguments
        if not isinstance(request, Mapping):
            raise ToolError.validation("the tool request must be a JSON object", pointer="")
        request = dict(request)
        if spec.name == "record_agent_activity" and "payload" in request:
            # Archive callers may include transport diagnostics inside an evidence payload.
            # Redact these before schema validation as well as before producer transport.
            from .activity import sanitize
            request["payload"] = sanitize(request["payload"])
        synthetic = request.get("synthetic") is True
        # 2. contract major
        _check_contract_major(inv, request)
        # 3. storage-like inputs, before any other work
        leaks = storage_input_problems({k:v for k,v in request.items() if k != "payload"} if spec.name == "record_agent_activity" else request)
        if leaks:
            raise ToolError.validation("storage locations, URIs, ARNs and path-like values are not accepted; use a trusted artifact reference", pointer=leaks[0])
        if spec.pre_validate is not None:  # tool-specific checks of the raw request (no producer call)
            spec.pre_validate(request)
        # tool-only fields (not in the pinned request schema) are checked by the tool, never forwarded
        tool_fields = {k: request.pop(k) for k in spec.tool_only_fields if k in request}
        # 4. input schema (pinned validators, incl. identifier formats and semantic checks)
        result = validate_document(request, spec.input_schema)
        if not result.valid:
            raise from_validation(result)
        if spec.name == "record_agent_activity":
            # The archived turn's correlation is the authenticated downstream transport
            # correlation too. Gateway invocation metadata otherwise supplies a new ID,
            # which the Platform correctly rejects as inconsistent evidence.
            cid = request["correlation_id"]
        if spec.request_writes(request) and "idempotency_key" not in request:
            raise ToolError.validation("write tools require an idempotency_key", pointer="/idempotency_key")
        # 5. environment
        env = runtime.settings.environment
        if inv.declared_environment is not None and inv.declared_environment != env:
            raise ToolError(FORBIDDEN, "the request targets another environment than this tool", reason="environment_mismatch")
        if env == "prod" and spec.state_changing and inv.source != "gateway":
            raise ToolError(FORBIDDEN, "state-changing tools are not directly invocable in prod", reason="prod_direct_write")
        if (spec.name == "run_portfolio_research" and request.get("dry_run") is False or spec.name == "resolve_portfolio_decision") and inv.source == "gateway":
            if runtime.gateway_research_authorizer is None:
                raise ToolError(FORBIDDEN, "paid research user verification is unavailable", reason="verified_user_token_required")
            verified = runtime.gateway_research_authorizer.verify_context(context)
            inv = replace(inv, identity=verified.identity, groups=verified.groups)
        if spec.authorize is not None:  # caller authorization, before any producer call
            spec.authorize(inv, request, tool_fields)
        # 6. dependency gating, then the tool itself
        if spec.gate_dependencies:
            for producer in spec.producers:
                check_producer_release(runtime.manifest(producer), producer, spec.entry.min_producer_contract)
        limits = runtime.limits()
        timeout = float(limits.timeouts_seconds.get(spec.entry.timeout_key, limits.timeouts_seconds.get("read", 15)))
        meta = CallMeta(correlation_id=cid, contract_version=contract_version(), caller=caller_block(inv, cid, synthetic=synthetic))
        ctx = ToolContext(spec, runtime, inv, cid, limits, meta, runtime.platform(timeout), runtime.jobs(timeout), synthetic=synthetic, tool_fields=tool_fields)
        ctx.record_ids(request)
        response = spec.run(ctx, request)
        if not isinstance(response, dict):
            raise ToolError.internal("the tool returned no response document")
        # 8. bound
        if spec.list_key and spec.list_key in response:
            response = ctx.bound(response, spec.list_key, truncated_key=spec.truncated_key)
        if response_size(response) > limits.response_max_bytes:
            raise ToolError.internal("the response exceeds the configured size limit", reason="response_too_large")
        # 9. output conformance and leak scan
        out = validate_document(response, spec.output_schema)
        if not out.valid:
            log.error("non-conformant response suppressed (correlation_id=%s tool=%s): %s", cid, spec.name, [i.message for i in out.issues][:10])
            raise ToolError.internal("the tool produced a non-conformant response")
        leaked = response_leaks(response, allowed_pointers=spec.grant_pointers)
        if leaked:
            log.error("response with storage-like values suppressed (correlation_id=%s tool=%s pointers=%s)", cid, spec.name, leaked[:10])
            raise ToolError.internal("the tool produced a response with a storage location")
        ctx.record_ids(response)
        return response
    except ToolError as err:
        outcome, retryable = err.code, err.retryable
        response = _envelope(err, cid, synthetic)
        return response
    except Exception:  # noqa: BLE001 - never leak a trace: full trace only in the log
        log.exception("unhandled tool failure (correlation_id=%s tool=%s)", cid, spec.name)
        err = ToolError.internal("unexpected failure")
        outcome, retryable = err.code, err.retryable
        response = _envelope(err, cid, synthetic)
        return response
    finally:
        if runtime.durable_activity_receipts and spec.name != "record_agent_activity":
            from .activity import persist_tool_receipt
            try:
                persist_tool_receipt(runtime, spec, inv, cid, request, response, outcome)
            except Exception:
                log.exception("durable tool receipt failed (correlation_id=%s tool=%s)", cid, spec.name)
                # Never report an unarchived successful decision. Idempotent write retries remain safe.
                if outcome == "OK" and isinstance(response, dict):
                    response.clear()
                    response.update(_envelope(ToolError.dependency_unavailable("the durable activity archive is unavailable", producer=PRODUCER_PLATFORM), cid, synthetic))
                    outcome = "DEPENDENCY_UNAVAILABLE"
        audit.emit(
            audit.audit_record(
                correlation_id=cid,
                tool=spec.name,
                caller_identity=inv.identity if inv else None,
                channel=inv.channel if inv else None,
                environment=runtime.settings.environment,
                contract_version=contract_version(),
                release_id=runtime.settings.release_id,
                outcome=outcome,
                retryable=retryable,
                downstream_ids=ctx.downstream_ids if ctx else None,
                duration_ms=(time.monotonic() - started) * 1000,
                untrusted=inv.untrusted if inv else None,
            )
        )
