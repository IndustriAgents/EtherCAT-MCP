from __future__ import annotations

from typing import Any

from pydantic import StrictInt

from ethercat_mcp._version import __version__


class DiagnosticsOperations:
    async def get_slave_info(self, slave_position: StrictInt) -> dict[str, Any]:
        """Return one slave from the last scan, with its AL state read fresh from the bus."""
        try:
            position = self._position(slave_position)
            entry, meta = await self.master.get_slave(position)
        except Exception as exc:
            return self._err(str(exc), {"slave_position": slave_position})
        return self._ok(data=entry, meta={**meta, "slave_position": position})

    async def test_slave_communication(self, slave_position: StrictInt) -> dict[str, Any]:
        """Check that a slave answers: read its AL state, then try a CoE read of object 0x1000."""
        try:
            position = self._position(slave_position)
            result, meta = await self.master.test_communication(position)
        except Exception as exc:
            return self._err(str(exc), {"slave_position": slave_position})
        return self._ok(data=result, meta={**meta, "slave_position": position})

    async def ping(self) -> dict[str, Any]:
        """Report the server version, backend, gates and slave-map status (no bus access)."""
        return self._ok(
            data={
                "server": "ethercat-mcp",
                "version": __version__,
                "backend": self.config.backend,
                "simulated": self.config.is_mock,
                "connection": self.master.connection_status(),
                "writes_enabled": self.config.writes_enabled,
                "state_change_enabled": self.config.state_change_enabled,
                "slave_aliases": self.resources.slave_map.count(),
                "slave_map_error": self.resources.slave_map.error,
            }
        )

    async def get_master_status(self) -> dict[str, Any]:
        """Report the master's configuration and runtime state (no bus access)."""
        return self._ok(data=self.master.connection_status())
