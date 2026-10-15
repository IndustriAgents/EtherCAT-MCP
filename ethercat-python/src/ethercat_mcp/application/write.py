from __future__ import annotations

from typing import Any

from pydantic import StrictInt

from ethercat_mcp.codec import encode_value, parse_bytes, parse_int
from ethercat_mcp.domain.errors import WRITTEN, parse_state
from ethercat_mcp.domain.model import MAX_PDO_BYTES


class WriteOperations:
    async def write_pdo(self, slave_position: StrictInt, offset: StrictInt, data: list[StrictInt]) -> dict[str, Any]:
        """Write output process data (RxPDO) bytes of one slave into the cyclic process image."""
        refused = self._writes_refused("write_pdo")
        if refused:
            return refused
        try:
            position = self._position(slave_position)
            start = parse_int(offset, "offset", minimum=0)
            payload = parse_bytes(data, max_length=MAX_PDO_BYTES)
            meta = await self.master.write_pdo(position, start, payload)
        except Exception as exc:
            return self._write_failed(exc, {"slave_position": slave_position})
        return self._ok(
            data={"slave_position": position, "offset": start, "written_bytes": len(payload)}, meta=self._written(meta)
        )

    async def write_sdo(
        self,
        slave_position: StrictInt,
        index: str | StrictInt,
        subindex: StrictInt,
        value: Any,
        data_type: str | None = None,
    ) -> dict[str, Any]:
        """Write a CoE object (SDO download) in a slave's object dictionary."""
        refused = self._writes_refused("write_sdo")
        if refused:
            return refused
        try:
            position = self._position(slave_position)
            idx = parse_int(index, "index", minimum=0, maximum=65535)
            sub = parse_int(subindex, "subindex", minimum=0, maximum=255)
            payload, dtype = encode_value(value, data_type)
            meta = await self.master.write_sdo(position, idx, sub, payload)
        except Exception as exc:
            return self._write_failed(exc, {"slave_position": slave_position})
        return self._ok(
            data={
                "slave_position": position,
                "index": f"0x{idx:04X}",
                "subindex": sub,
                "written": value,
                "data_type": dtype,
                "raw_hex": payload.hex(),
            },
            meta=self._written(meta),
        )

    async def set_slave_state(self, slave_position: StrictInt, state: str) -> dict[str, Any]:
        """Request an AL state for one slave and wait until it is reached or ETHERCAT_TIMEOUT expires."""
        if not self.config.state_changes_allowed:
            return self._not_sent(
                (
                    "State changes are disabled (they need both "
                    "ETHERCAT_WRITES_ENABLED=true and "
                    "ETHERCAT_STATE_CHANGE_ENABLED=true)"
                ),
                {"tool": "set_slave_state"},
            )
        try:
            position = self._position(slave_position)
            desired = parse_state(state)
            result, meta = await self.master.set_slave_state(position, desired)
        except Exception as exc:
            return self._write_failed(exc, {"slave_position": slave_position, "state": state})
        outcome = result.pop("outcome", WRITTEN)
        return self._ok(data={"slave_position": position, **result}, meta=self._written({**meta, "outcome": outcome}))

    async def write_slave_by_alias(self, alias: str, data: list[StrictInt]) -> dict[str, Any]:
        """Write output process data for an alias from SLAVE_MAP_FILE (same rules and gate as write_pdo)."""
        refused = self._writes_refused("write_slave_by_alias")
        if refused:
            return refused
        try:
            position, offset, _ = self.resources.slave_map.resolve(alias)
        except ValueError as exc:
            return self._not_sent(str(exc), {"alias": alias})
        result = await self.write_pdo(position, offset, data)
        result["meta"]["alias"] = alias
        return result
