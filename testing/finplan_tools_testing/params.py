"""SSM ``get_parameter`` double (dict-backed) with the boto3 error shape for absent parameters."""

from __future__ import annotations

import json
from typing import Any


class ParameterNotFound(Exception):
    def __init__(self, name: str) -> None:
        super().__init__(f"ParameterNotFound: {name}")
        self.response = {"Error": {"Code": "ParameterNotFound", "Message": "parameter not found"}}


class DictParameterStore:
    """``get_parameter(Name=...)`` over a dict; counts reads (to assert TTL caching)."""

    def __init__(self, values: dict[str, Any] | None = None) -> None:
        self.values: dict[str, str] = {}
        self.reads: list[str] = []
        for k, v in (values or {}).items():
            self.put(k, v)

    def put(self, name: str, value: Any) -> None:
        self.values[name] = value if isinstance(value, str) else json.dumps(value, sort_keys=True)

    def delete(self, name: str) -> None:
        self.values.pop(name, None)

    def get_parameter(self, Name: str, WithDecryption: bool = False) -> dict[str, Any]:  # noqa: N803 - boto3 shape
        self.reads.append(Name)
        if Name not in self.values:
            raise ParameterNotFound(Name)
        return {"Parameter": {"Name": Name, "Type": "String", "Value": self.values[Name]}}
