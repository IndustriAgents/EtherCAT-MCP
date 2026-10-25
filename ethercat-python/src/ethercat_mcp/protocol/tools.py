from __future__ import annotations

from typing import Any

from pydantic import StrictInt

from ethercat_mcp.application.resources import ToolResources

from ..application.operations import Operations
from .support import READ_ONLY, SCAN, WRITES, FastMCP


def register_tools(server: FastMCP, resources: ToolResources) -> None:
    operations = Operations(resources, lambda: None)

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
        return await operations.scan_network()

    @server.tool(annotations=READ_ONLY)
    async def get_slave_info(slave_position: StrictInt) -> dict[str, Any]:
        """Return one slave from the last scan, with its AL state read fresh from the bus.

        Args:
            slave_position: 0-based position on the bus, as reported by scan_network.
        """
        return await operations.get_slave_info(slave_position)

    @server.tool(annotations=READ_ONLY)
    async def test_slave_communication(slave_position: StrictInt) -> dict[str, Any]:
        """Check that a slave answers: read its AL state, then try a CoE read of object 0x1000.

        success is false if the slave does not answer the state read. A slave without
        a mailbox (CoE) still passes; data.coe says whether the SDO read worked.

        Args:
            slave_position: 0-based position on the bus.
        """
        return await operations.test_slave_communication(slave_position)

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
        return await operations.read_pdo(slave_position, offset, length)

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
        return await operations.write_pdo(slave_position, offset, data)

    @server.tool(annotations=READ_ONLY)
    async def read_sdo(
        slave_position: StrictInt,
        index: str | StrictInt,
        subindex: StrictInt = 0,
        data_type: str | None = None,
        max_bytes: StrictInt = 256,
    ) -> dict[str, Any]:
        """Read a CoE object (SDO upload) from a slave's object dictionary.

        The slave must have a mailbox and be in PREOP, SAFEOP or OP.
        Uploads larger than the selected buffer fail; no partial object or retry is returned.

        Args:
            slave_position: 0-based position on the bus.
            index: Object index, e.g. "0x1018" (hex needs the 0x prefix; "1018" is decimal).
            subindex: Subindex 0-255 (default 0).
            data_type: How to decode the bytes: BOOL, INT8, UINT8, INT16, UINT16, INT32, UINT32,
                INT64, UINT64, REAL32, REAL64, STRING or BYTES (IEC names such as UDINT also work).
                Without it, 1/2/4/8-byte values are decoded as unsigned integers.
            max_bytes: Native upload buffer size, 1-4096 (default 256). Larger buffers
                require enough remaining mailbox deadline; this is not a requested prefix.
        """
        return await operations.read_sdo(slave_position, index, subindex, data_type, max_bytes)

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
        return await operations.write_sdo(slave_position, index, subindex, value, data_type)

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
        return await operations.set_slave_state(slave_position, state)

    @server.tool(annotations=READ_ONLY)
    async def list_slaves() -> dict[str, Any]:
        """List the slave aliases defined in SLAVE_MAP_FILE (no bus access).

        Each entry has alias, position, offset, length and description. Entries that
        cannot be used carry an error.
        """
        return await operations.list_slaves()

    @server.tool(annotations=READ_ONLY)
    async def read_slave_by_alias(alias: str, length: StrictInt | None = None) -> dict[str, Any]:
        """Read input process data for an alias from SLAVE_MAP_FILE (same rules as read_pdo).

        Args:
            alias: Alias name from SLAVE_MAP_FILE.
            length: Number of bytes. Default: the alias's length, else to the end of the input image.
        """
        return await operations.read_slave_by_alias(alias, length)

    @server.tool(annotations=WRITES)
    async def write_slave_by_alias(alias: str, data: list[StrictInt]) -> dict[str, Any]:
        """Write output process data for an alias from SLAVE_MAP_FILE (same rules and gate as write_pdo).

        Args:
            alias: Alias name from SLAVE_MAP_FILE.
            data: Byte values 0-255 to write at the alias's offset.
        """
        return await operations.write_slave_by_alias(alias, data)

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
        return await operations.load_esi_file(filepath, device_type, revision)

    @server.tool(annotations=READ_ONLY)
    async def ping() -> dict[str, Any]:
        """Report the server version, backend, gates and slave-map status (no bus access)."""
        return await operations.ping()

    @server.tool(annotations=READ_ONLY)
    async def get_master_status() -> dict[str, Any]:
        """Report the master's configuration and runtime state (no bus access).

        Includes the backend, interface or mock endpoint, gates, slaves found by the last
        scan, and on a real bus whether the process image is mapped, the cyclic exchange
        is running, and the last working counter.
        """
        return await operations.get_master_status()

    from .contract import install_contract

    install_contract(server)
