"""EtherCAT MCP tool definitions.

Every tool returns the IndustriConnect envelope ``{success, data, error, meta}``.
``meta.backend`` names the backend that answered (``pysoem`` for a real bus,
``mock`` for the simulator, which also sets ``meta.simulated``).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import Any

import anyio
from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations
from pydantic import StrictInt

from ._version import __version__
from .codec import decode_value, encode_value, parse_bytes, parse_int
from .config import EthercatConfig
from .ec_master import NOT_SENT, WRITTEN, EthercatMaster, parse_state
from .esi_parser import ESIParser

READ_ONLY = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False)
# A scan does not drive outputs, but SOEM resets every slave on the segment to INIT and then PRE-OP.
SCAN = ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=False, openWorldHint=False)
MAX_PDO_BYTES = 4096  # SOEM's process image (EC_IOMAPSIZE) is 4096 bytes
ESI_DEADLINE_S = 60.0
WRITES = ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=False, openWorldHint=False)


class SlaveMap:
    """Aliases from ``SLAVE_MAP_FILE``, reloaded when the file changes."""

    def __init__(self, path: Path | None) -> None:
        self.path = path
        self._slaves: dict[str, Any] = {}
        self._stamp: tuple[int, int] | None = None
        self.error: str | None = None
        self.refresh()

    def refresh(self) -> None:
        if not self.path:
            self._slaves, self._stamp, self.error = {}, None, None
            return
        try:
            stat = self.path.stat()
        except OSError as exc:
            self._slaves, self._stamp = {}, None
            self.error = f"SLAVE_MAP_FILE {self.path} cannot be read: {exc.strerror or exc}"
            return
        stamp = (stat.st_mtime_ns, stat.st_size)
        if stamp == self._stamp:
            return
        self._stamp = stamp
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            self._slaves = {}
            self.error = f"SLAVE_MAP_FILE {self.path} is not valid JSON: {exc}"
            return
        if not isinstance(data, dict):
            self._slaves = {}
            self.error = f"SLAVE_MAP_FILE {self.path} must hold a JSON object of alias -> {{position, offset}}"
            return
        self._slaves = {str(alias): spec for alias, spec in data.items()}
        self.error = None

    def count(self) -> int:
        self.refresh()
        return len(self._slaves)

    def resolve(self, alias: str) -> tuple[int, int, int | None]:
        """Return ``(position, offset, length)`` for ``alias``; raise ``ValueError`` with the reason."""
        self.refresh()
        if self.error:
            raise ValueError(self.error)
        if alias not in self._slaves:
            raise ValueError(f"Unknown alias {alias!r}")
        spec = self._slaves[alias]
        if not isinstance(spec, dict):
            raise ValueError(f"Alias {alias!r} in SLAVE_MAP_FILE must be an object with a position")
        if "position" not in spec:
            raise ValueError(f"Alias {alias!r} in SLAVE_MAP_FILE has no position")
        position = parse_int(spec["position"], f"position of alias {alias!r}", minimum=0)
        offset = parse_int(spec.get("offset", 0), f"offset of alias {alias!r}", minimum=0)
        length = spec.get("length")
        length = None if length is None else parse_int(length, f"length of alias {alias!r}", minimum=1)
        return position, offset, length

    def list(self) -> list[dict[str, Any]]:
        self.refresh()
        entries = []
        for alias, spec in self._slaves.items():
            entry: dict[str, Any] = {"alias": alias}
            if isinstance(spec, dict):
                entry.update(
                    {
                        "position": spec.get("position"),
                        "offset": spec.get("offset", 0),
                        "length": spec.get("length"),
                        "description": spec.get("description"),
                        "vendor_id": spec.get("vendor_id"),
                    }
                )
            try:
                self.resolve(alias)
            except ValueError as exc:
                entry["error"] = str(exc)
            entries.append(entry)
        return entries


@dataclass(slots=True)
class ToolResources:
    master: EthercatMaster
    config: EthercatConfig
    slave_map: SlaveMap
    esi_parser: ESIParser


def envelope(success: bool, data: Any = None, error: str | None = None, meta: dict[str, Any] | None = None) -> dict:
    return {"success": success, "data": data, "error": error, "meta": meta or {}}


def register_tools(server: FastMCP, resources: ToolResources) -> None:
    master = resources.master
    config = resources.config

    def _meta(extra: dict[str, Any] | None = None) -> dict[str, Any]:
        return {**master.describe(), **(extra or {})}

    def _ok(data: Any = None, meta: dict[str, Any] | None = None) -> dict[str, Any]:
        return envelope(True, data=data, meta=_meta(meta))

    def _err(message: str, meta: dict[str, Any] | None = None) -> dict[str, Any]:
        return envelope(False, error=message, meta=_meta(meta))

    def _position(value: Any) -> int:
        return parse_int(value, "slave_position", minimum=0)

    def _not_sent(message: str, meta: dict[str, Any] | None = None) -> dict[str, Any]:
        return _err(message, {**(meta or {}), "outcome": NOT_SENT, "request_sent": False})

    def _write_failed(exc: Exception, meta: dict[str, Any]) -> dict[str, Any]:
        """Envelope for a device-changing request that failed: say whether it may have been applied."""
        outcome = getattr(exc, "outcome", NOT_SENT)
        return _err(str(exc), {**meta, "outcome": outcome, "request_sent": outcome != NOT_SENT})

    def _written(meta: dict[str, Any]) -> dict[str, Any]:
        return {**meta, "outcome": meta.get("outcome", WRITTEN), "request_sent": True}

    def _writes_refused(tool: str) -> dict[str, Any] | None:
        if not config.writes_enabled:
            return _not_sent(
                "Write operations are disabled (set ETHERCAT_WRITES_ENABLED=true to allow them)", {"tool": tool}
            )
        return None

    # -- bus discovery ----------------------------------------------------

    @server.tool(annotations=SCAN)
    async def scan_network() -> dict[str, Any]:
        """Enumerate the slaves on the bus (PySOEM config_init) and return their identity and state.

        Not passive: SOEM resets every slave on the segment to INIT and then requests
        PRE-OP while it enumerates. Before that, one broadcast read checks the segment:
        if any slave is in SAFEOP or OP (another master may be running it), the scan is
        refused unless both ETHERCAT_WRITES_ENABLED and ETHERCAT_STATE_CHANGE_ENABLED are
        true. A rescan is always refused while a slave this server brought up is in
        SAFEOP or OP.
        Returns position, name, vendor ID, product code, revision, AL state and the
        process-data sizes (null on a real bus until the process image is mapped);
        meta.segment_state_before and meta.reset_live_segment say what the scan reset.
        """
        try:
            devices, meta = await master.scan_slaves()
        except Exception as exc:
            return _write_failed(exc, {})
        return _ok(data={"slaves": devices}, meta=_written(meta))

    @server.tool(annotations=READ_ONLY)
    async def get_slave_info(slave_position: StrictInt) -> dict[str, Any]:
        """Return one slave from the last scan, with its AL state read fresh from the bus.

        Args:
            slave_position: 0-based position on the bus, as reported by scan_network.
        """
        try:
            position = _position(slave_position)
            entry, meta = await master.get_slave(position)
        except Exception as exc:
            return _err(str(exc), {"slave_position": slave_position})
        return _ok(data=entry, meta={**meta, "slave_position": position})

    @server.tool(annotations=READ_ONLY)
    async def test_slave_communication(slave_position: StrictInt) -> dict[str, Any]:
        """Check that a slave answers: read its AL state, then try a CoE read of object 0x1000.

        success is false if the slave does not answer the state read. A slave without
        a mailbox (CoE) still passes; data.coe says whether the SDO read worked.

        Args:
            slave_position: 0-based position on the bus.
        """
        try:
            position = _position(slave_position)
            result, meta = await master.test_communication(position)
        except Exception as exc:
            return _err(str(exc), {"slave_position": slave_position})
        return _ok(data=result, meta={**meta, "slave_position": position})

    # -- process data -------------------------------------------------------

    @server.tool(annotations=READ_ONLY)
    async def read_pdo(
        slave_position: StrictInt, offset: StrictInt = 0, length: StrictInt | None = None
    ) -> dict[str, Any]:
        """Read input process data (TxPDO) bytes of one slave from the cyclic process image.

        Inputs only exist once the process image is mapped and are only valid while the
        slave is in SAFEOP or OP (use set_slave_state first). Errors otherwise, and also
        when the last frame's working counter shows that it was lost or (with every
        process-data slave in SAFEOP/OP) that a slave did not process it. For a slave
        with fewer than 8 input bits, only its own bits are returned, shifted to bit 0
        (meta.bit_packed).

        Args:
            slave_position: 0-based position on the bus.
            offset: First byte within the slave's input image (default 0).
            length: Number of bytes (default: to the end of the slave's input image).
        """
        meta: dict[str, Any] = {"slave_position": slave_position}
        try:
            position = _position(slave_position)
            start = parse_int(offset, "offset", minimum=0)
            count = None if length is None else parse_int(length, "length", minimum=1)
            payload, meta = await master.read_pdo(position, start, count)
        except Exception as exc:
            return _err(str(exc), meta)
        return _ok(
            data={"slave_position": position, "offset": start, "length": len(payload), "raw_data_hex": payload.hex()},
            meta=meta,
        )

    @server.tool(annotations=WRITES)
    async def write_pdo(slave_position: StrictInt, offset: StrictInt, data: list[StrictInt]) -> dict[str, Any]:
        """Write output process data (RxPDO) bytes of one slave into the cyclic process image.

        Needs ETHERCAT_WRITES_ENABLED=true (off by default). The slave must be in OP,
        because outputs are not applied in any other state. The bytes stay in the image
        and go out with every cycle; the request is never repeated. success means they
        went out and were confirmed: by the full expected working counter when every
        process-data slave is in OP (meta.verified "expected_wkc"), otherwise by the
        target still being in OP without error (meta.verified "slave_state"). If that
        cannot be confirmed, meta.outcome is "unknown": read back before retrying.
        Refused for slaves whose outputs are bit-packed into a byte shared with others.

        Args:
            slave_position: 0-based position on the bus.
            offset: First byte within the slave's output image.
            data: Byte values 0-255 to write starting at offset.
        """
        refused = _writes_refused("write_pdo")
        if refused:
            return refused
        try:
            position = _position(slave_position)
            start = parse_int(offset, "offset", minimum=0)
            payload = parse_bytes(data, max_length=MAX_PDO_BYTES)
            meta = await master.write_pdo(position, start, payload)
        except Exception as exc:
            return _write_failed(exc, {"slave_position": slave_position})
        return _ok(
            data={"slave_position": position, "offset": start, "written_bytes": len(payload)}, meta=_written(meta)
        )

    # -- CoE / SDO ----------------------------------------------------------

    @server.tool(annotations=READ_ONLY)
    async def read_sdo(
        slave_position: StrictInt, index: str | StrictInt, subindex: StrictInt = 0, data_type: str | None = None
    ) -> dict[str, Any]:
        """Read a CoE object (SDO upload) from a slave's object dictionary.

        The slave must have a mailbox and be in PREOP, SAFEOP or OP.

        Args:
            slave_position: 0-based position on the bus.
            index: Object index, e.g. "0x1018" (hex needs the 0x prefix; "1018" is decimal).
            subindex: Subindex 0-255 (default 0).
            data_type: How to decode the bytes: BOOL, INT8, UINT8, INT16, UINT16, INT32, UINT32,
                INT64, UINT64, REAL32, REAL64, STRING or BYTES (IEC names such as UDINT also work).
                Without it, 1/2/4/8-byte values are decoded as unsigned integers.
        """
        meta: dict[str, Any] = {"slave_position": slave_position}
        try:
            position = _position(slave_position)
            idx = parse_int(index, "index", minimum=0, maximum=0xFFFF)
            sub = parse_int(subindex, "subindex", minimum=0, maximum=0xFF)
            raw, meta = await master.read_sdo(position, idx, sub)
            decoded = decode_value(raw, data_type)
        except Exception as exc:
            return _err(str(exc), meta)
        return _ok(data={"slave_position": position, "index": f"0x{idx:04X}", "subindex": sub, **decoded}, meta=meta)

    @server.tool(annotations=WRITES)
    async def write_sdo(
        slave_position: StrictInt, index: str | StrictInt, subindex: StrictInt, value: Any, data_type: str | None = None
    ) -> dict[str, Any]:
        """Write a CoE object (SDO download) in a slave's object dictionary.

        Needs ETHERCAT_WRITES_ENABLED=true (off by default). success means the slave
        acknowledged the download. The download is never repeated: if the answer is
        lost, meta.outcome is "unknown" (read back before retrying); an SDO abort is
        "rejected".

        Args:
            slave_position: 0-based position on the bus.
            index: Object index, e.g. "0x8000".
            subindex: Subindex 0-255.
            value: The value to write. A number, true/false, a string, or a list of byte values.
            data_type: Encoding of value: BOOL, INT8, UINT8, INT16, UINT16, INT32, UINT32, INT64,
                UINT64, REAL32, REAL64, STRING or BYTES. Required unless value is a list of bytes.
        """
        refused = _writes_refused("write_sdo")
        if refused:
            return refused
        try:
            position = _position(slave_position)
            idx = parse_int(index, "index", minimum=0, maximum=0xFFFF)
            sub = parse_int(subindex, "subindex", minimum=0, maximum=0xFF)
            payload, dtype = encode_value(value, data_type)
            meta = await master.write_sdo(position, idx, sub, payload)
        except Exception as exc:
            return _write_failed(exc, {"slave_position": slave_position})
        return _ok(
            data={
                "slave_position": position,
                "index": f"0x{idx:04X}",
                "subindex": sub,
                "written": value,
                "data_type": dtype,
                "raw_hex": payload.hex(),
            },
            meta=_written(meta),
        )

    # -- state machine ------------------------------------------------------

    @server.tool(annotations=WRITES)
    async def set_slave_state(slave_position: StrictInt, state: str) -> dict[str, Any]:
        """Request an AL state for one slave and wait until it is reached or ETHERCAT_TIMEOUT expires.

        Needs both ETHERCAT_WRITES_ENABLED=true and ETHERCAT_STATE_CHANGE_ENABLED=true
        (both off by default). On a real bus, the first request for SAFEOP or OP maps the
        process image (zeroed) and starts the cyclic process-data exchange. A slave's
        outputs are zeroed in the image before it enters OP and whenever it ends up
        outside OP, so stale outputs are never re-applied. A pending AL error on the
        slave is acknowledged. Follow the EtherCAT state machine: INIT -> PREOP ->
        SAFEOP -> OP. success means the slave reports the requested state; a refusal is
        meta.outcome "rejected", a timeout "unknown".

        Args:
            slave_position: 0-based position on the bus.
            state: INIT, PREOP, SAFEOP or OP.
        """
        if not config.state_changes_allowed:
            return _not_sent(
                "State changes are disabled (they need both ETHERCAT_WRITES_ENABLED=true and "
                "ETHERCAT_STATE_CHANGE_ENABLED=true)",
                {"tool": "set_slave_state"},
            )
        try:
            position = _position(slave_position)
            desired = parse_state(state)
            result, meta = await master.set_slave_state(position, desired)
        except Exception as exc:
            return _write_failed(exc, {"slave_position": slave_position, "state": state})
        outcome = result.pop("outcome", WRITTEN)
        return _ok(data={"slave_position": position, **result}, meta=_written({**meta, "outcome": outcome}))

    # -- slave aliases --------------------------------------------------------

    @server.tool(annotations=READ_ONLY)
    async def list_slaves() -> dict[str, Any]:
        """List the slave aliases defined in SLAVE_MAP_FILE (no bus access).

        Each entry has alias, position, offset, length and description. Entries that
        cannot be used carry an error.
        """
        slaves = resources.slave_map.list()
        if resources.slave_map.error:
            return _err(resources.slave_map.error)
        return _ok(data={"slaves": slaves, "count": len(slaves)})

    @server.tool(annotations=READ_ONLY)
    async def read_slave_by_alias(alias: str, length: StrictInt | None = None) -> dict[str, Any]:
        """Read input process data for an alias from SLAVE_MAP_FILE (same rules as read_pdo).

        Args:
            alias: Alias name from SLAVE_MAP_FILE.
            length: Number of bytes. Default: the alias's length, else to the end of the input image.
        """
        try:
            position, offset, map_length = resources.slave_map.resolve(alias)
            count = map_length if length is None else parse_int(length, "length", minimum=1)
        except ValueError as exc:
            return _err(str(exc), {"alias": alias})
        result = await read_pdo(position, offset, count)
        result["meta"]["alias"] = alias
        return result

    @server.tool(annotations=WRITES)
    async def write_slave_by_alias(alias: str, data: list[StrictInt]) -> dict[str, Any]:
        """Write output process data for an alias from SLAVE_MAP_FILE (same rules and gate as write_pdo).

        Args:
            alias: Alias name from SLAVE_MAP_FILE.
            data: Byte values 0-255 to write at the alias's offset.
        """
        refused = _writes_refused("write_slave_by_alias")
        if refused:
            return refused
        try:
            position, offset, _ = resources.slave_map.resolve(alias)
        except ValueError as exc:
            return _not_sent(str(exc), {"alias": alias})
        result = await write_pdo(position, offset, data)
        result["meta"]["alias"] = alias
        return result

    # -- files and status ---------------------------------------------------------

    @server.tool(annotations=READ_ONLY)
    async def load_esi_file(
        filepath: str, device_type: str | None = None, revision: str | None = None
    ) -> dict[str, Any]:
        """Parse an ESI (EtherCAT Slave Information) XML file (no bus access).

        Returns the vendor and, for a single matching device, its object dictionary
        (index, name, type, subindexes) and Tx/Rx PDO mappings. When several devices
        match, returns a summary list instead.

        Args:
            filepath: Path to the file, or a file name inside ETHERCAT_ESI_PATH.
            device_type: Device type to pick, e.g. "EL1008".
            revision: Revision to pick, e.g. "0x00120000".
        """
        try:
            # Parsing can take a while for a large file: keep it off the event loop, bounded.
            with anyio.fail_after(ESI_DEADLINE_S):
                data = await anyio.to_thread.run_sync(
                    partial(resources.esi_parser.select, filepath, device_type, revision), abandon_on_cancel=True
                )
        except TimeoutError:
            return _err(f"Parsing {filepath} did not finish within {ESI_DEADLINE_S:.0f} s", {"filepath": filepath})
        except Exception as exc:
            return _err(str(exc), {"filepath": filepath})
        return _ok(data=data)

    @server.tool(annotations=READ_ONLY)
    async def ping() -> dict[str, Any]:
        """Report the server version, backend, gates and slave-map status (no bus access)."""
        return _ok(
            data={
                "server": "ethercat-mcp",
                "version": __version__,
                "backend": config.backend,
                "simulated": config.is_mock,
                "connection": master.connection_status(),
                "writes_enabled": config.writes_enabled,
                "state_change_enabled": config.state_change_enabled,
                "slave_aliases": resources.slave_map.count(),
                "slave_map_error": resources.slave_map.error,
            }
        )

    @server.tool(annotations=READ_ONLY)
    async def get_master_status() -> dict[str, Any]:
        """Report the master's configuration and runtime state (no bus access).

        Includes the backend, interface or mock endpoint, gates, slaves found by the last
        scan, and on a real bus whether the process image is mapped, the cyclic exchange
        is running, and the last working counter.
        """
        return _ok(data=master.connection_status())
