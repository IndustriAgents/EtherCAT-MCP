from __future__ import annotations

from functools import partial
from typing import Any

import anyio

from ethercat_mcp.domain.model import ESI_DEADLINE_S


class MetadataOperations:
    async def list_slaves(self) -> dict[str, Any]:
        """List the slave aliases defined in SLAVE_MAP_FILE (no bus access)."""
        slaves = self.resources.slave_map.list()
        if self.resources.slave_map.error:
            return self._err(self.resources.slave_map.error)
        return self._ok(data={"slaves": slaves, "count": len(slaves)})

    async def load_esi_file(
        self, filepath: str, device_type: str | None = None, revision: str | None = None
    ) -> dict[str, Any]:
        """Parse an ESI (EtherCAT Slave Information) XML file (no bus access)."""
        try:
            with anyio.fail_after(ESI_DEADLINE_S):
                data = await anyio.to_thread.run_sync(
                    partial(self.resources.esi_parser.select, filepath, device_type, revision), abandon_on_cancel=True
                )
        except TimeoutError:
            return self._err(f"Parsing {filepath} did not finish within {ESI_DEADLINE_S:.0f} s", {"filepath": filepath})
        except Exception as exc:
            return self._err(str(exc), {"filepath": filepath})
        return self._ok(data=data)
