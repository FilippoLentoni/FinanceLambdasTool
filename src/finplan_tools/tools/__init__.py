"""Tool implementations: one module per tool, each registering itself with
:func:`finplan_tools.core.registry.register_tool`.

:func:`load_all` imports every module of this package, so adding a tool means adding a module;
nothing here needs editing.
"""

from __future__ import annotations

import importlib
import pkgutil

__all__ = ["load_all"]

_loaded = False


def load_all() -> None:
    """Import every tool module (idempotent)."""
    global _loaded
    if _loaded:
        return
    for mod in sorted(pkgutil.iter_modules(__path__), key=lambda m: m.name):
        if not mod.name.startswith("_"):
            importlib.import_module(f"{__name__}.{mod.name}")
    _loaded = True
