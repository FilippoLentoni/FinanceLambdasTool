"""Shared offline fixtures (FOUNDATION-owned; the tool and infra suites build on these).

* Offline suites (``tests/unit``, ``tests/contract``) never see real credentials
  (``tests/offline_env.py``); deployed suites run by the stage runner keep the stage role's.
* ``offline`` gives the real pipeline wired to the in-process mocks (``finplan_tools_testing``) in
  beta with both producers released; ``offline_factory(env=..., with_model=..., ...)`` builds others.
* ``invoke(offline, tool, arguments, **event_meta)`` runs a registered tool through the handler path.
* ``no_network`` makes any socket connection fail.
"""

from __future__ import annotations

import socket
from typing import Any, Callable, Iterator

import pytest

from tests.offline_env import apply_offline_environment, deployed_suite_mode

if not deployed_suite_mode():
    apply_offline_environment()

from finplan_tools_testing.runtime import Offline, offline_runtime  # noqa: E402


@pytest.fixture
def offline_factory() -> Callable[..., Offline]:
    return offline_runtime


@pytest.fixture
def offline() -> Offline:
    return offline_runtime("beta")


@pytest.fixture
def invoke() -> Callable[..., dict[str, Any]]:
    from finplan_tools.handler import invoke as _invoke

    def run(o: Offline, tool: str, arguments: Any, **meta: Any) -> dict[str, Any]:
        return _invoke(tool, o.event(arguments, **meta), None, o.runtime)

    return run


@pytest.fixture
def no_network(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    def refuse(*a: Any, **k: Any) -> Any:
        raise OSError("network access is disabled in offline tests")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    yield
