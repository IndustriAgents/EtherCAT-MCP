from __future__ import annotations

from typing import Any

from ethercat_mcp.codec import parse_int
from ethercat_mcp.domain.errors import NOT_SENT, WRITTEN
from ethercat_mcp.domain.model import envelope


class CommonOperations:
    def __init__(self, resources, cancel_scope):
        self.resources = resources
        self.cancel_scope = cancel_scope
        self.master = self.resources.master
        self.config = self.resources.config

    def _meta(self, extra: dict[str, Any] | None = None) -> dict[str, Any]:
        return {**self.master.describe(), **(extra or {})}

    def _ok(self, data: Any = None, meta: dict[str, Any] | None = None) -> dict[str, Any]:
        return envelope(True, data=data, meta=self._meta(meta))

    def _err(self, message: str, meta: dict[str, Any] | None = None) -> dict[str, Any]:
        return envelope(False, error=message, meta=self._meta(meta))

    def _position(self, value: Any) -> int:
        return parse_int(value, "slave_position", minimum=0)

    def _not_sent(self, message: str, meta: dict[str, Any] | None = None) -> dict[str, Any]:
        return self._err(message, {**(meta or {}), "outcome": NOT_SENT, "request_sent": False})

    def _write_failed(self, exc: Exception, meta: dict[str, Any]) -> dict[str, Any]:
        """Envelope for a device-changing request that failed: say whether it may have been applied."""
        outcome = getattr(exc, "outcome", NOT_SENT)
        return self._err(str(exc), {**meta, "outcome": outcome, "request_sent": outcome != NOT_SENT})

    def _written(self, meta: dict[str, Any]) -> dict[str, Any]:
        return {**meta, "outcome": meta.get("outcome", WRITTEN), "request_sent": True}

    def _writes_refused(self, tool: str) -> dict[str, Any] | None:
        if not self.config.writes_enabled:
            return self._not_sent(
                "Write operations are disabled (set ETHERCAT_WRITES_ENABLED=true to allow them)", {"tool": tool}
            )
        return None
