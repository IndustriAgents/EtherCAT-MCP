"""Mock EtherCAT slaves behind a newline-delimited JSON API over TCP.

This is not an EtherCAT device: it cannot answer raw EtherCAT frames. It
simulates a small segment (EK1100 coupler, EL1008 digital inputs, EL2008
digital outputs, EL3102 analog inputs) closely enough to exercise every tool of
the EtherCAT MCP server through its mock transport (``ETHERCAT_BACKEND=mock``):

* the AL state machine (INIT -> PREOP -> SAFEOP -> OP, invalid transitions are
  refused with AL status 0x0011),
* process data: inputs readable in SAFEOP/OP, outputs applied only in OP and
  cleared when a slave leaves OP. The EL2008 outputs are wired back to the
  EL1008 inputs, so a write can be read back,
* CoE (SDO) objects on the EL3102, with SDO abort codes for unknown objects,
  read-only objects and wrong lengths. The other three terminals have no
  mailbox, as on real hardware.

Values are simplified and the identity data is illustrative. Uses only the
Python standard library.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import math
import os
import signal
import struct
import sys
import time
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger("ethercat_mock_slave")

INIT, PREOP, BOOT, SAFEOP, OP = 0x01, 0x02, 0x03, 0x04, 0x08
STATE_NAMES = {INIT: "INIT", PREOP: "PREOP", BOOT: "BOOT", SAFEOP: "SAFEOP", OP: "OP"}
STATE_CODES = {name: code for code, name in STATE_NAMES.items() if code != BOOT}

AL_NO_ERROR = 0x0000
AL_INVALID_STATE_CHANGE = 0x0011
AL_TEXT = {AL_NO_ERROR: "", AL_INVALID_STATE_CHANGE: "Invalid requested state change"}

VALID_TRANSITIONS = {
    (INIT, PREOP),
    (PREOP, SAFEOP),
    (SAFEOP, OP),
    (PREOP, INIT),
    (SAFEOP, INIT),
    (OP, INIT),
    (SAFEOP, PREOP),
    (OP, PREOP),
    (OP, SAFEOP),
}

ABORT_READ_ONLY = 0x06010002
ABORT_NO_OBJECT = 0x06020000
ABORT_LENGTH = 0x06070010
ABORT_NO_SUBINDEX = 0x06090011
ABORT_TEXT = {
    ABORT_READ_ONLY: "Attempt to write a read only object",
    ABORT_NO_OBJECT: "The object does not exist in the object directory",
    ABORT_LENGTH: "Data type does not match, length of service parameter does not match",
    ABORT_NO_SUBINDEX: "Subindex does not exist",
}

BECKHOFF = 0x00000002
MAX_LINE = 64 * 1024


class MockError(Exception):
    """A request the simulated slave refuses. Becomes ``{"success": false}``."""

    def __init__(self, message: str, **extra: Any) -> None:
        super().__init__(message)
        self.extra = extra


@dataclass
class SdoEntry:
    name: str
    data: bytes
    writable: bool = False


@dataclass
class MockConfig:
    host: str = "127.0.0.1"
    port: int = 6700
    update_interval: float = 1.0
    verbose: bool = False


@dataclass
class MockSlave:
    position: int
    name: str
    product_code: int
    revision: int
    vendor_id: int = BECKHOFF
    serial: int = 0
    input_size: int = 0
    output_size: int = 0
    has_mailbox: bool = False
    state: int = INIT
    al_status_code: int = AL_NO_ERROR
    inputs: bytearray = field(default_factory=bytearray)
    outputs: bytearray = field(default_factory=bytearray)
    objects: dict[tuple[int, int], SdoEntry] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.inputs = bytearray(self.input_size)
        self.outputs = bytearray(self.output_size)

    @property
    def state_name(self) -> str:
        return STATE_NAMES.get(self.state, f"0x{self.state:02X}")

    def describe(self) -> dict[str, Any]:
        return {
            "position": self.position,
            "name": self.name,
            "vendor_id": f"0x{self.vendor_id:08X}",
            "product_code": f"0x{self.product_code:08X}",
            "revision": f"0x{self.revision:08X}",
            "input_size": self.input_size,
            "output_size": self.output_size,
            "has_mailbox": self.has_mailbox,
            **self.state_entry(),
        }

    def state_entry(self) -> dict[str, Any]:
        return {
            "position": self.position,
            "state": self.state_name,
            "state_code": self.state,
            "al_status_code": self.al_status_code,
            "al_status": AL_TEXT.get(self.al_status_code, ""),
        }


def _identity_objects(slave: MockSlave, device_type: int) -> dict[tuple[int, int], SdoEntry]:
    return {
        (0x1000, 0): SdoEntry("Device type", struct.pack("<I", device_type)),
        (0x1008, 0): SdoEntry("Device name", slave.name.encode()),
        (0x1009, 0): SdoEntry("Hardware version", b"00"),
        (0x100A, 0): SdoEntry("Software version", b"01"),
        (0x1018, 0): SdoEntry("Identity: number of entries", struct.pack("<B", 4)),
        (0x1018, 1): SdoEntry("Vendor ID", struct.pack("<I", slave.vendor_id)),
        (0x1018, 2): SdoEntry("Product code", struct.pack("<I", slave.product_code)),
        (0x1018, 3): SdoEntry("Revision", struct.pack("<I", slave.revision)),
        (0x1018, 4): SdoEntry("Serial number", struct.pack("<I", slave.serial)),
    }


def default_slaves() -> list[MockSlave]:
    coupler = MockSlave(0, "EK1100", product_code=0x044C2C52, revision=0x00110000)
    digital_in = MockSlave(1, "EL1008", product_code=0x03F03052, revision=0x00120000, input_size=1)
    digital_out = MockSlave(2, "EL2008", product_code=0x07D83052, revision=0x00120000, output_size=1)
    analog_in = MockSlave(
        3, "EL3102", product_code=0x0C1E3052, revision=0x00140000, serial=1234, input_size=8, has_mailbox=True
    )
    analog_in.objects = _identity_objects(analog_in, 0x01011389)
    for channel, base in ((1, 0x8000), (2, 0x8010)):
        analog_in.objects[(base, 0x06)] = SdoEntry(f"AI Settings Ch.{channel}: Enable filter", b"\x00", True)
        analog_in.objects[(base, 0x15)] = SdoEntry(f"AI Settings Ch.{channel}: Filter settings", b"\x00\x00", True)
    return [coupler, digital_in, digital_out, analog_in]


def _int(request: dict[str, Any], key: str, default: int | None = None, minimum: int = 0, maximum: int = 0xFFFF) -> int:
    value = request.get(key, default)
    if value is None:
        raise MockError(f"Missing field {key!r}")
    if isinstance(value, bool) or not isinstance(value, int):
        raise MockError(f"Field {key!r} must be an integer")
    if not minimum <= value <= maximum:
        raise MockError(f"Field {key!r}={value} is out of range ({minimum}..{maximum})")
    return value


class EthercatMockServer:
    """The simulated segment plus the TCP/JSON server around it."""

    def __init__(self, config: MockConfig, slaves: list[MockSlave] | None = None) -> None:
        self.config = config
        self.slaves = slaves if slaves is not None else default_slaves()
        self._server: asyncio.base_events.Server | None = None
        self._update_task: asyncio.Task[None] | None = None
        self._started = time.monotonic()
        self.update_inputs()

    # -- lifecycle ------------------------------------------------------------

    async def start(self) -> None:
        if self._server is not None:
            return
        self._server = await asyncio.start_server(
            self._handle_client, self.config.host, self.config.port, limit=MAX_LINE
        )
        sockets = self._server.sockets or []
        if sockets:
            self.config.port = sockets[0].getsockname()[1]
        for sock in sockets:
            host, port = sock.getsockname()[:2]
            logger.info("EtherCAT mock listening on %s:%s", host, port)
        self._update_task = asyncio.create_task(self._update_loop())

    async def stop(self) -> None:
        if self._update_task is not None:
            self._update_task.cancel()
            try:
                await self._update_task
            except asyncio.CancelledError:
                pass
            self._update_task = None
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

    async def _update_loop(self) -> None:
        while True:
            await asyncio.sleep(self.config.update_interval)
            self.update_inputs()
            logger.debug("Updated simulated analog inputs")

    # -- simulation -------------------------------------------------------------

    def update_inputs(self) -> None:
        """Move the EL3102 analog values (a slow sine/cosine pair)."""
        t = time.monotonic() - self._started
        for slave in self.slaves:
            if slave.name == "EL3102":
                ch1 = int(16000 * math.sin(t / 5))
                ch2 = int(8000 * math.cos(t / 7))
                slave.inputs[:] = struct.pack("<HhHh", 0, ch1, 0, ch2)
                slave.objects[(0x6000, 0x11)] = SdoEntry("AI Inputs Ch.1: Value", struct.pack("<h", ch1))
                slave.objects[(0x6010, 0x11)] = SdoEntry("AI Inputs Ch.2: Value", struct.pack("<h", ch2))

    def _loopback(self) -> None:
        """EL2008 outputs are wired to EL1008 inputs. Outputs only drive anything in OP."""
        outputs = next((s for s in self.slaves if s.name == "EL2008"), None)
        inputs = next((s for s in self.slaves if s.name == "EL1008"), None)
        if outputs is not None and inputs is not None:
            inputs.inputs[:] = outputs.outputs if outputs.state == OP else bytes(len(inputs.inputs))

    def _slave(self, request: dict[str, Any]) -> MockSlave:
        position = _int(request, "position", maximum=len(self.slaves) - 1 if self.slaves else 0)
        if not self.slaves:
            raise MockError("No slaves on the simulated segment")
        return self.slaves[position]

    def _set_state(self, slave: MockSlave, target: int) -> None:
        if target == slave.state:
            slave.al_status_code = AL_NO_ERROR
            return
        if (slave.state, target) not in VALID_TRANSITIONS:
            slave.al_status_code = AL_INVALID_STATE_CHANGE
            raise MockError(
                f"Slave {slave.position} ({slave.name}): invalid requested state change "
                f"{slave.state_name} -> {STATE_NAMES[target]} (AL status 0x0011); it stays in {slave.state_name}",
                **slave.state_entry(),
            )
        if slave.state == OP:
            slave.outputs[:] = bytes(len(slave.outputs))  # outputs fall back to their safe state
        slave.state = target
        slave.al_status_code = AL_NO_ERROR

    def _mailbox(self, slave: MockSlave) -> None:
        if not slave.has_mailbox:
            raise MockError(f"Slave {slave.position} ({slave.name}) has no mailbox: CoE (SDO) is not supported")
        if slave.state == INIT:
            raise MockError(f"Slave {slave.position} ({slave.name}) is in INIT: the mailbox needs PREOP or higher")

    def _sdo(self, slave: MockSlave, request: dict[str, Any]) -> tuple[tuple[int, int], SdoEntry]:
        index = _int(request, "index", maximum=0xFFFF)
        subindex = _int(request, "subindex", default=0, maximum=0xFF)
        key = (index, subindex)
        if key in slave.objects:
            return key, slave.objects[key]
        code = ABORT_NO_SUBINDEX if any(i == index for i, _ in slave.objects) else ABORT_NO_OBJECT
        raise MockError(
            f"SDO abort 0x{code:08X} on 0x{index:04X}:{subindex}: {ABORT_TEXT[code]}",
            abort_code=f"0x{code:08X}",
        )

    def dispatch(self, request: Any) -> dict[str, Any]:
        """Handle one request and return the reply (never raises)."""
        try:
            if not isinstance(request, dict):
                raise MockError("Request must be a JSON object")
            return {"success": True, "data": self._dispatch(request)}
        except MockError as exc:
            return {"success": False, "error": str(exc), **exc.extra}
        except Exception as exc:  # keep the connection alive on bugs
            logger.exception("Unexpected error handling %r", request)
            return {"success": False, "error": f"Internal mock error: {exc}"}

    def _dispatch(self, request: dict[str, Any]) -> Any:
        op = request.get("op")
        if op == "ping":
            return {"mock": True, "slaves": len(self.slaves)}
        if op == "scan":
            # Like SOEM's config_init: reset every slave to INIT, then request PRE-OP.
            for slave in self.slaves:
                slave.outputs[:] = bytes(len(slave.outputs))
                slave.state = PREOP
                slave.al_status_code = AL_NO_ERROR
            self._loopback()
            return [slave.describe() for slave in self.slaves]
        if op == "read_state":
            return [slave.state_entry() for slave in self.slaves]
        if op == "set_state":
            slave = self._slave(request)
            if "state_code" in request:
                target = _int(request, "state_code", maximum=0xFF)
            else:
                name = str(request.get("state", "")).upper()
                if name not in STATE_CODES:
                    raise MockError(f"Unknown state {request.get('state')!r} (use INIT, PREOP, SAFEOP or OP)")
                target = STATE_CODES[name]
            if target not in STATE_NAMES or target == BOOT:
                raise MockError(f"State 0x{target:02X} is not supported by the mock")
            self._set_state(slave, target)
            self._loopback()
            return slave.state_entry()
        if op == "read_pdo":
            slave = self._slave(request)
            if slave.state not in (SAFEOP, OP):
                raise MockError(
                    f"Slave {slave.position} ({slave.name}) is in {slave.state_name}: "
                    "inputs are only valid in SAFEOP or OP"
                )
            if not slave.inputs:
                raise MockError(f"Slave {slave.position} ({slave.name}) has no input process data")
            self._loopback()
            offset = _int(request, "offset", default=0)
            length = _int(request, "length", default=len(slave.inputs) - offset if offset < len(slave.inputs) else 1)
            if length < 1 or offset + length > len(slave.inputs):
                raise MockError(
                    f"Slave {slave.position} has {len(slave.inputs)} input byte(s); "
                    f"offset {offset} with length {length} is out of range"
                )
            data = bytes(slave.inputs[offset : offset + length])
            return {"position": slave.position, "raw_data_hex": data.hex(), "state": slave.state_name}
        if op == "write_pdo":
            slave = self._slave(request)
            if slave.state != OP:
                raise MockError(
                    f"Slave {slave.position} ({slave.name}) is in {slave.state_name}: outputs are only applied in OP"
                )
            if not slave.outputs:
                raise MockError(f"Slave {slave.position} ({slave.name}) has no output process data")
            offset = _int(request, "offset", default=0)
            data = request.get("data")
            if not isinstance(data, list) or not data:
                raise MockError("Field 'data' must be a non-empty list of byte values")
            if any(isinstance(b, bool) or not isinstance(b, int) or not 0 <= b <= 0xFF for b in data):
                raise MockError("Field 'data' must only hold byte values 0-255")
            if offset + len(data) > len(slave.outputs):
                raise MockError(
                    f"Slave {slave.position} has {len(slave.outputs)} output byte(s); "
                    f"offset {offset} with length {len(data)} is out of range"
                )
            slave.outputs[offset : offset + len(data)] = bytes(data)
            self._loopback()
            return {"position": slave.position, "written_bytes": len(data), "state": slave.state_name}
        if op == "read_sdo":
            slave = self._slave(request)
            self._mailbox(slave)
            _, entry = self._sdo(slave, request)
            return {"position": slave.position, "data_hex": entry.data.hex(), "size": len(entry.data)}
        if op == "write_sdo":
            slave = self._slave(request)
            self._mailbox(slave)
            key, entry = self._sdo(slave, request)
            try:
                payload = bytes.fromhex(str(request.get("data_hex", "")))
            except ValueError:
                raise MockError("Field 'data_hex' must be a hex string") from None
            if not entry.writable:
                raise MockError(
                    f"SDO abort 0x{ABORT_READ_ONLY:08X} on 0x{key[0]:04X}:{key[1]}: {ABORT_TEXT[ABORT_READ_ONLY]}",
                    abort_code=f"0x{ABORT_READ_ONLY:08X}",
                )
            if len(payload) != len(entry.data):
                raise MockError(
                    f"SDO abort 0x{ABORT_LENGTH:08X} on 0x{key[0]:04X}:{key[1]}: {ABORT_TEXT[ABORT_LENGTH]} "
                    f"(expected {len(entry.data)} byte(s), got {len(payload)})",
                    abort_code=f"0x{ABORT_LENGTH:08X}",
                )
            entry.data = payload
            return {"position": slave.position, "written_bytes": len(payload)}
        raise MockError(f"Unknown op {op!r}")

    # -- network --------------------------------------------------------------

    async def _handle_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        peer = writer.get_extra_info("peername")
        logger.debug("Client connected %s", peer)
        try:
            while True:
                try:
                    line = await reader.readline()
                except (asyncio.LimitOverrunError, ValueError):
                    await self._send(writer, {"success": False, "error": f"Request longer than {MAX_LINE} bytes"})
                    break
                if not line:
                    break
                line = line.strip()
                if not line:
                    continue
                try:
                    request = json.loads(line)
                except json.JSONDecodeError:
                    response: dict[str, Any] = {"success": False, "error": "Invalid JSON"}
                else:
                    response = self.dispatch(request)
                    logger.debug("%s -> %s", request, response)
                await self._send(writer, response)
        except ConnectionError:
            pass
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except ConnectionError:
                pass
            logger.debug("Client disconnected %s", peer)

    @staticmethod
    async def _send(writer: asyncio.StreamWriter, message: dict[str, Any]) -> None:
        writer.write(json.dumps(message).encode("utf-8") + b"\n")
        await writer.drain()


def _env_bool(name: str) -> bool:
    return os.getenv(name, "false").strip().lower() in {"1", "true", "yes", "on"}


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="ethercat-mock-slave",
        description="Simulated EtherCAT slaves behind a newline-delimited JSON API over TCP "
        "(for the EtherCAT MCP server's ETHERCAT_BACKEND=mock).",
    )
    parser.add_argument("--host", default=os.getenv("MOCK_ETHERCAT_HOST", "127.0.0.1"), help="default 127.0.0.1")
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.getenv("MOCK_ETHERCAT_PORT", "6700")),
        help="default 6700; 0 picks a free port (the log line shows which)",
    )
    parser.add_argument(
        "--update-interval",
        type=float,
        default=float(os.getenv("MOCK_ETHERCAT_UPDATE_INTERVAL", "1.0")),
        help="seconds between analog input updates (default 1.0)",
    )
    parser.add_argument(
        "--verbose", action="store_true", default=_env_bool("MOCK_ETHERCAT_VERBOSE"), help="log every request"
    )
    args = parser.parse_args(argv)
    if args.update_interval <= 0:
        parser.error("--update-interval must be greater than 0")
    return args


async def run_server(config: MockConfig) -> None:
    server = EthercatMockServer(config)
    await server.start()
    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop_event.set)
        except NotImplementedError:  # pragma: no cover - Windows
            pass
    await stop_event.wait()
    logger.info("Stopping EtherCAT mock")
    await server.stop()


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        stream=sys.stderr,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    config = MockConfig(host=args.host, port=args.port, update_interval=args.update_interval, verbose=args.verbose)
    try:
        asyncio.run(run_server(config))
    except KeyboardInterrupt:  # pragma: no cover
        pass


if __name__ == "__main__":
    main()
