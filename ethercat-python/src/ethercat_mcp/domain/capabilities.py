"""Static backend support; never a probe or an authorization decision."""

from __future__ import annotations

from typing import Any


def capabilities(backend: str, *, writes_enabled: bool, extra_gate: bool = False) -> dict[str, Any]:
    operations: dict[str, dict[str, Any]] = {}
    for name in (
        "read_pdo",
        "write_pdo",
        "scan_network",
        "get_slave_info",
        "test_slave_communication",
        "read_sdo",
        "write_sdo",
        "set_slave_state",
        "list_slaves",
        "load_esi_file",
        "ping",
    ):
        supported = backend in ("pysoem", "mock") or name in ("list_slaves", "load_esi_file", "ping")
        reason = None
        if not supported and reason is None:
            reason = "No supported native backend for this operation"
        enabled = supported
        if name in ("write_pdo", "write_sdo"):
            enabled = supported and writes_enabled
        if name in ("set_slave_state",):
            enabled = supported and writes_enabled and extra_gate
        if supported and not enabled:
            reason = "Disabled by native configuration gates"
        operations[name] = {"supported": supported, "enabled": enabled}
        if reason is not None:
            operations[name]["reason"] = reason
    return {
        "protocol": "ethercat",
        "backend": backend,
        "simulated": backend == "mock",
        "scope": "backend_support",
        "device_verified": False,
        "operations": operations,
        "prerequisites": [
            "Native configuration gates do not grant MCP policy or device permission.",
            "PySOEM requires an exclusive compatible interface and raw Ethernet privileges.",
            "Scan, mapping, valid slave state and cyclic exchange are required for PDO access.",
            "The host cycle is not a certified real-time or safety controller.",
        ],
    }
