"""Validated deployment policy, independent of MCP and native libraries."""

from __future__ import annotations

from dataclasses import dataclass, fields


@dataclass(frozen=True)
class Policy:
    read_only: bool = False
    allowed_tools: tuple[str, ...] | None = None
    allowed_operations: tuple[str, ...] | None = None
    max_in_flight: int = 64
    max_request_bytes: int = 1048576
    audit_path: str | None = None

    @classmethod
    def from_dict(cls, value):
        if not isinstance(value, dict) or set(value) - {f.name for f in fields(cls)}:
            raise ValueError("Policy must be an object containing only documented fields")
        data = dict(value)
        if "read_only" in data and type(data["read_only"]) is not bool:
            raise ValueError("read_only must be a boolean")
        for name, low, high in [("max_in_flight", 1, 256), ("max_request_bytes", 1024, 16777216)]:
            if name in data and (type(data[name]) is not int or not low <= data[name] <= high):
                raise ValueError(f"{name} must be an integer between {low} and {high}")
        for name in ["allowed_tools", "allowed_operations"]:
            if name in data and data[name] is not None:
                if not isinstance(data[name], list) or any(not isinstance(v, str) or not v for v in data[name]):
                    raise ValueError(f"{name} must be an array of nonempty strings or null")
                data[name] = tuple(data[name])
        if data.get("audit_path") is not None and (not isinstance(data["audit_path"], str) or not data["audit_path"]):
            raise ValueError("audit_path must be a nonempty path or null")
        return cls(**data)
