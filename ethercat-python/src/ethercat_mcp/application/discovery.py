from __future__ import annotations

from typing import Any


class DiscoveryOperations:
    async def scan_network(self) -> dict[str, Any]:
        """Enumerate the slaves on the bus (PySOEM config_init) and return their identity and state."""
        try:
            devices, meta = await self.master.scan_slaves()
        except Exception as exc:
            return self._write_failed(exc, {})
        return self._ok(data={"slaves": devices}, meta=self._written(meta))
