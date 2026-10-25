from __future__ import annotations

from typing import Any

from pydantic import StrictInt

from ethercat_mcp.codec import decode_value, normalize_data_type, parse_int, validate_upload_size


class ReadOperations:
    async def read_pdo(
        self, slave_position: StrictInt, offset: StrictInt = 0, length: StrictInt | None = None
    ) -> dict[str, Any]:
        """Read input process data (TxPDO) bytes of one slave from the cyclic process image."""
        meta: dict[str, Any] = {"slave_position": slave_position}
        try:
            position = self._position(slave_position)
            start = parse_int(offset, "offset", minimum=0)
            count = None if length is None else parse_int(length, "length", minimum=1)
            payload, meta = await self.master.read_pdo(position, start, count)
        except Exception as exc:
            return self._err(str(exc), meta)
        return self._ok(
            data={"slave_position": position, "offset": start, "length": len(payload), "raw_data_hex": payload.hex()},
            meta=meta,
        )

    async def read_sdo(
        self,
        slave_position: StrictInt,
        index: str | StrictInt,
        subindex: StrictInt = 0,
        data_type: str | None = None,
        max_bytes: StrictInt = 256,
    ) -> dict[str, Any]:
        """Read a CoE object (SDO upload) from a slave's object dictionary."""
        meta: dict[str, Any] = {"slave_position": slave_position}
        try:
            position = self._position(slave_position)
            idx = parse_int(index, "index", minimum=0, maximum=65535)
            sub = parse_int(subindex, "subindex", minimum=0, maximum=255)
            dtype = normalize_data_type(data_type)
            limit = validate_upload_size(max_bytes, dtype)
            raw, meta = await self.master.read_sdo(position, idx, sub, limit)
            decoded = decode_value(raw, dtype)
        except Exception as exc:
            return self._err(str(exc), meta)
        return self._ok(
            data={"slave_position": position, "index": f"0x{idx:04X}", "subindex": sub, **decoded}, meta=meta
        )

    async def read_slave_by_alias(self, alias: str, length: StrictInt | None = None) -> dict[str, Any]:
        """Read input process data for an alias from SLAVE_MAP_FILE (same rules as read_pdo)."""
        try:
            position, offset, map_length = self.resources.slave_map.resolve(alias)
            count = map_length if length is None else parse_int(length, "length", minimum=1)
        except ValueError as exc:
            return self._err(str(exc), {"alias": alias})
        result = await self.read_pdo(position, offset, count)
        result["meta"]["alias"] = alias
        return result
