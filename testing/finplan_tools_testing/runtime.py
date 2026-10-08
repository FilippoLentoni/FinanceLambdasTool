"""Offline runtime: the real pipeline and clients wired to the in-process mocks (no AWS, no network)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timezone
from typing import Any

from finplan_tools.backends.jobs import JobClient
from finplan_tools.backends.platform import PlatformClient
from finplan_tools.core.config import Settings, ToolLimits
from finplan_tools.core.contracts import contract_major, contract_version
from finplan_tools.core.identity import INVOCATION_KEY
from finplan_tools.core.pipeline import Runtime
from finplan_tools.core.references import ReferenceResolver

from ._base import Clock, fixture, require_valid
from .mock_jobs import MockJobApi
from .mock_platform import MockPlatform
from .params import DictParameterStore

RELEASE_ID = "rel_01KDVDNAZ83BAMMYCEGWF33DPM"


def release_manifest(repo: str, env: str, *, served_majors: list[int] | None = None) -> dict[str, Any]:
    """A contract-valid release manifest of ``repo`` in ``env`` (synthetic)."""
    doc = fixture("release-manifest", "gamma-financelambdastool")
    outputs = {
        "financialplanning": {"plan-endpoint": f"/finplan/{env}/financialplanning/api/plan-endpoint", "ingestion-endpoint": f"/finplan/{env}/financialplanning/api/ingestion-endpoint"},
        "financemodel": {"job-endpoint": f"/finplan/{env}/financemodel/api/job-endpoint"},
        "financelambdastool": {"tool-catalog": f"/finplan/{env}/financelambdastool/contract/tool-catalog"},
    }[repo]
    doc.update(repo=repo, environment=env, outputs=outputs, contract_version=contract_version(), served_contract_majors=list(served_majors if served_majors is not None else [contract_major()]))
    doc.pop("approved_by", None)
    doc.pop("approved_at", None)
    doc.pop("rolled_back_from", None)
    if env == "prod":
        approved = fixture("release-manifest", "prod-financialplanning-approved")
        doc.update(approved_by=approved["approved_by"], approved_at=approved["approved_at"])
    return require_valid(doc, "release-manifest")


@dataclass
class Offline:
    runtime: Runtime
    platform: MockPlatform
    jobs: MockJobApi
    params: DictParameterStore
    clock: Clock

    @property
    def env(self) -> str:
        return self.runtime.settings.environment

    def event(self, arguments: Any, *, source: str = "direct_test", environment: str | None = None, tool: str | None = None, **meta: Any) -> dict[str, Any]:
        """A direct-invocation event (see ``finplan_tools.core.identity``)."""
        inv: dict[str, Any] = {"source": source, "environment": environment or self.env, **meta}
        ev: dict[str, Any] = {INVOCATION_KEY: inv, "arguments": arguments}
        if tool:
            ev["tool"] = tool
        return ev

    def set_param(self, repo: str, category: str, name: str, value: Any, *, shared: bool = False) -> None:
        self.params.put(f"/finplan/{'shared' if shared else self.env}/{repo}/{category}/{name}", value)
        if self.runtime.references is not None:
            self.runtime.references.invalidate()

    def remove_model(self) -> None:
        """No FinanceModel release in this environment (experiment tools -> DEPENDENCY_UNAVAILABLE)."""
        self.params.delete(f"/finplan/{self.env}/financemodel/release/manifest")
        self.runtime.references.invalidate()  # type: ignore[union-attr]


def offline_runtime(
    env: str = "beta",
    *,
    with_model: bool = True,
    with_platform: bool = True,
    platform_majors: list[int] | None = None,
    model_majors: list[int] | None = None,
    tool_limits: dict[str, Any] | None = None,
    release_id: str | None = RELEASE_ID,
    tool_name: str | None = None,
) -> Offline:
    clock = Clock()
    platform = MockPlatform(clock=clock)
    jobs = MockJobApi(clock=clock)
    jobs.environment = env
    params = DictParameterStore()
    if with_platform:
        params.put(f"/finplan/{env}/financialplanning/release/manifest", release_manifest("financialplanning", env, served_majors=platform_majors))
    if with_model:
        params.put(f"/finplan/{env}/financemodel/release/manifest", release_manifest("financemodel", env, served_majors=model_majors))
    if tool_limits is not None:
        params.put(f"/finplan/{env}/financelambdastool/config/tool-limits", tool_limits)
    refs = ReferenceResolver(params, env, ttl_seconds=300)
    runtime = Runtime(
        settings=Settings(environment=env, release_id=release_id, tool_name=tool_name),
        references=refs,
        platform=lambda timeout: PlatformClient(platform, platform, timeout=timeout),
        jobs=lambda timeout: JobClient(jobs, timeout=timeout),
        clock=lambda: clock.now().astimezone(timezone.utc),
    )
    return Offline(runtime, platform, jobs, params, clock)


__all__ = ["Offline", "RELEASE_ID", "offline_runtime", "release_manifest", "ToolLimits"]
