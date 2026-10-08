"""Access to the pinned ``finplan-contracts`` package (the only source of schemas).

Every schema, error code and identifier rule comes from the installed, digest-pinned package
(``contracts-pin.json``). This repository never copies a schema file (CS-01): tool schemas are
looked up by name (``tools/get-plan-request``) and referenced by their ``$id``.
"""

from __future__ import annotations

import re
from functools import lru_cache
from typing import Any

from finplan_contracts.schemas import SchemaStore, load_store
from finplan_contracts.validate import ValidationResult, validate

__all__ = [
    "contract_version",
    "contract_major",
    "served_majors",
    "store",
    "schema_id",
    "validate_document",
    "registered_error_codes",
    "parse_major",
]

_SEMVER_MAJOR = re.compile(r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)(?:[-+].*)?\Z")


@lru_cache(maxsize=1)
def store() -> SchemaStore:
    """The schema store of the installed (pinned) contract package."""
    return load_store()


def contract_version() -> str:
    """The pinned contract package version, for example ``1.0.0``."""
    return store().version


def parse_major(version: Any) -> int | None:
    """Major of a semver string, or None when it is not a semantic version."""
    if not isinstance(version, str):
        return None
    m = _SEMVER_MAJOR.match(version)
    return int(m.group(1)) if m else None


def contract_major() -> int:
    major = parse_major(contract_version())
    if major is None:  # pragma: no cover - the package version is always semver
        raise RuntimeError("pinned contract version is not semver")
    return major


def served_majors() -> list[int]:
    """Contract majors this release serves. One pin serves exactly its own major."""
    return [contract_major()]


def schema_id(name: str) -> str:
    """``$id`` of a schema in the pinned package (raises ``SchemaNotFound`` for unknown names)."""
    return store().get(name).id


def validate_document(document: Any, schema: str, **context: Any) -> ValidationResult:
    """Validate against a pinned schema, including its ``x-finplan-checks`` semantic checks."""
    return validate(document, schema, context=context or None, store=store())


@lru_cache(maxsize=1)
def registered_error_codes() -> dict[str, dict[str, Any]]:
    return dict(store().get("error-codes").schema["x-finplan-error-codes"])
