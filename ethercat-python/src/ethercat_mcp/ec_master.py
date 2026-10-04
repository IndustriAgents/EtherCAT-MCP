"""EtherCAT master: a PySOEM backend, a mock-bridge backend, and an async facade.

The MCP tools talk to :class:`EthercatMaster`. It picks a backend from the
configuration (``ETHERCAT_BACKEND``), enforces the write and state-change gates,
and runs the blocking backend calls in worker threads.

``PysoemBackend`` drives a real bus through PySOEM. The calls it makes are the
ones PySOEM 1.1.12 exposes (``Master.open/config_init/config_map/read_state/
send_processdata/receive_processdata``, ``CdefSlave.sdo_read/sdo_write/
write_state/state_check`` and the ``input``/``output`` process-image views).
"""

from __future__ import annotations

import logging
import struct
import threading
import time
from collections.abc import Callable
from functools import partial
from typing import Any, Protocol, TypeVar

import anyio

from .config import EthercatConfig

logger = logging.getLogger(__name__)

T = TypeVar("T")

# EtherCAT application-layer (AL) states, as used by SOEM and PySOEM.
NONE_STATE = 0x00
INIT_STATE = 0x01
PREOP_STATE = 0x02
BOOT_STATE = 0x03
SAFEOP_STATE = 0x04
OP_STATE = 0x08
STATE_ERROR = 0x10  # error indicator in AL status
STATE_ACK = 0x10  # error acknowledge in AL control

STATE_NAMES = {
    NONE_STATE: "NONE",
    INIT_STATE: "INIT",
    PREOP_STATE: "PREOP",
    BOOT_STATE: "BOOT",
    SAFEOP_STATE: "SAFEOP",
    OP_STATE: "OP",
}
SETTABLE_STATES = {"INIT": INIT_STATE, "PREOP": PREOP_STATE, "SAFEOP": SAFEOP_STATE, "OP": OP_STATE}
_STATE_ALIASES = {
    "PRE-OP": "PREOP",
    "PRE_OP": "PREOP",
    "PREOPERATIONAL": "PREOP",
    "PRE-OPERATIONAL": "PREOP",
    "SAFE-OP": "SAFEOP",
    "SAFE_OP": "SAFEOP",
    "SAFEOPERATIONAL": "SAFEOP",
    "SAFE-OPERATIONAL": "SAFEOP",
    "OPERATIONAL": "OP",
}

# CdefSlave.state_check holds the GIL while it polls, which stalls the process-data
# thread. Poll in short slices and sleep (GIL released) in between.
_STATE_POLL_US = 1000
_STATE_POLL_SLEEP_S = 0.01

# ESC FMMU registers (ETG.1000.4): 16 bytes each from 0x0600. SOEM's config_map
# writes process-data FMMUs (type 2 = outputs, 1 = inputs) starting at FMMU0.
_FMMU_BASE = 0x0600
_FMMU_COUNT = 4
_FMMU_FORMAT = "<IHBBHBBB"  # LogStart, LogLength, LogStartbit, LogEndbit, PhysStart, PhysStartbit, Type, Active


def state_name(code: int | None) -> str:
    """Render an AL state code, e.g. ``0x14`` -> ``"SAFEOP+ERROR"``."""
    if code is None:
        return "UNKNOWN"
    base = code & 0x0F
    name = STATE_NAMES.get(base, f"0x{base:02X}")
    return f"{name}+ERROR" if code & STATE_ERROR else name


def parse_state(state: str) -> int:
    """Map ``INIT``/``PREOP``/``SAFEOP``/``OP`` (and common spellings) to a state code."""
    key = state.strip().upper() if isinstance(state, str) else ""
    key = _STATE_ALIASES.get(key, key)
    if key not in SETTABLE_STATES:
        raise ValueError(f"Invalid state {state!r} (expected one of INIT, PREOP, SAFEOP, OP)")
    return SETTABLE_STATES[key]


class EthercatMasterError(RuntimeError):
    """An EtherCAT operation failed. The message is shown to the client."""


class GateError(EthercatMasterError):
    """A write or state change was refused because its gate is disabled."""


class Backend(Protocol):
    """What :class:`EthercatMaster` needs from a backend. All calls are blocking."""

    name: str

    @property
    def scanned(self) -> bool: ...

    def describe(self) -> dict[str, Any]: ...
    def scan(self) -> list[dict[str, Any]]: ...
    def slave_count(self) -> int: ...
    def slave_entry(self, position: int) -> dict[str, Any]: ...
    def read_states(self) -> list[dict[str, Any]]: ...
    def read_pdo(self, position: int, offset: int, length: int | None) -> tuple[bytes, dict[str, Any]]: ...
    def write_pdo(self, position: int, offset: int, data: bytes) -> dict[str, Any]: ...
    def read_sdo(self, position: int, index: int, subindex: int) -> bytes: ...
    def write_sdo(self, position: int, index: int, subindex: int, data: bytes) -> None: ...
    def set_state(self, position: int, state: int) -> dict[str, Any]: ...
    def status(self) -> dict[str, Any]: ...
    def close(self) -> None: ...


def refuse_rescan(states: list[dict[str, Any]]) -> None:
    """Raise if a slave is in SAFEOP or OP: a scan would reset it to INIT."""
    active = [s for s in states if (s["state_code"] & 0x0F) in (SAFEOP_STATE, OP_STATE)]
    if active:
        where = ", ".join(f"{s['position']} ({s['state']})" for s in active)
        raise EthercatMasterError(
            f"Refusing to rescan: slave(s) {where} are exchanging process data, and a scan resets every "
            "slave to INIT. Bring them to PREOP or INIT with set_slave_state first."
        )


def check_slice(image: bytes, offset: int, length: int | None, kind: str, position: int) -> bytes:
    """Cut ``length`` bytes at ``offset`` out of a process-image view, or explain why not."""
    size = len(image)
    if size == 0:
        raise EthercatMasterError(f"Slave {position} has no {kind} process data")
    if length is None:
        length = size - offset
    if offset >= size or length < 1 or offset + length > size:
        raise EthercatMasterError(
            f"Slave {position} has {size} {kind} byte(s); offset {offset} with length {length} is out of range"
        )
    return bytes(image[offset : offset + length])


# ---------------------------------------------------------------------------
# PySOEM backend (real bus)
# ---------------------------------------------------------------------------


class PysoemBackend:
    """Real EtherCAT master on ``ETHERCAT_INTERFACE`` through PySOEM.

    Life cycle on the bus:

    * ``scan()`` runs ``config_init``. SOEM resets every slave to INIT and then
      requests PRE-OP; that is how SOEM enumerates a segment.
    * The process image is mapped (``config_map``) only when a slave is first
      asked for SAFEOP or OP through ``set_state`` (the state-change gate).
      Mapping runs with ``manual_state_change`` set, so SOEM does not push
      every slave to SAFE-OP on its own. A background thread then exchanges
      process data every ``ETHERCAT_CYCLE_TIME`` microseconds.
    * ``close()`` stops the exchange, requests INIT for all slaves if process
      data was running, and closes the interface.

    SOEM packs slaves with fewer than 8 bits of process data into shared bytes,
    and PySOEM's ``input``/``output`` views return the whole shared byte without
    the bit offset. The backend therefore reads the slave's FMMU registers after
    mapping: byte-aligned images are used as they are, bit-packed inputs are
    masked to the slave's own bits, and writes to bit-packed outputs are refused
    because they would change a neighbour's outputs.
    """

    name = "pysoem"

    def __init__(self, config: EthercatConfig, pysoem_module: Any | None = None) -> None:
        self.config = config
        self._pysoem = pysoem_module
        self._lock = threading.RLock()  # serialises acyclic access: open, scan, map, SDO, state
        self._pd_lock = threading.Lock()  # guards the process-data exchange and the output image
        self._cycle_cv = threading.Condition()
        self._master: Any | None = None
        self._slaves: list[Any] = []
        self._scanned = False
        self._mapped = False
        self._io_map_size: int | None = None
        self._cycle_thread: threading.Thread | None = None
        self._cycle_stop = threading.Event()
        self._cycle_count = 0
        self._last_wkc: int | None = None
        self._cycle_error: str | None = None
        self._layouts: dict[int, dict[str, Any] | None] = {}

    # -- helpers -----------------------------------------------------------

    def _lib(self) -> Any:
        if self._pysoem is None:
            try:
                import pysoem
            except Exception as exc:  # pragma: no cover - depends on the platform build
                raise EthercatMasterError(
                    f"PySOEM cannot be imported ({exc}). Run `uv sync` in ethercat-python/."
                ) from exc
            self._pysoem = pysoem
        return self._pysoem

    def _ensure_open(self) -> Any:
        if self._master is not None:
            return self._master
        master = self._lib().Master()
        try:
            master.open(self.config.interface)
        except Exception as exc:
            raise EthercatMasterError(f"Failed to open interface {self.config.interface}: {exc}") from exc
        self._master = master
        return master

    def _require_scanned(self) -> Any:
        if not self._scanned or self._master is None:
            raise EthercatMasterError("No bus scan yet: call scan_network first")
        return self._master

    def _require_slave(self, position: int) -> Any:
        self._require_scanned()
        if not 0 <= position < len(self._slaves):
            raise EthercatMasterError(
                f"Slave position {position} is out of range (the last scan found {len(self._slaves)} slave(s))"
            )
        return self._slaves[position]

    def _al_text(self, code: int) -> str:
        try:
            return str(self._lib().al_status_code_to_string(code))
        except Exception:  # pragma: no cover - defensive
            return ""

    def _state_entry(self, position: int, slave: Any) -> dict[str, Any]:
        code = int(slave.state)
        al = int(slave.al_status)
        return {
            "position": position,
            "state": state_name(code),
            "state_code": code,
            "al_status_code": f"0x{al:04X}",
            "al_status": self._al_text(al) if al else "",
        }

    def _describe(self, position: int, slave: Any) -> dict[str, Any]:
        entry = {
            "position": position,
            "name": slave.name,
            "vendor_id": f"0x{int(slave.man):08X}",
            "product_code": f"0x{int(slave.id):08X}",
            "revision": f"0x{int(slave.rev):08X}",
            # Sizes are only known once the process image is mapped.
            "input_size": len(slave.input) if self._mapped else None,
            "output_size": len(slave.output) if self._mapped else None,
        }
        entry.update(self._state_entry(position, slave))
        return entry

    def _require_state(self, position: int, slave: Any, allowed: tuple[int, ...], why: str) -> None:
        code = int(slave.state)
        if code & STATE_ERROR or (code & 0x0F) not in allowed:
            wanted = " or ".join(STATE_NAMES[s] for s in allowed)
            raise EthercatMasterError(
                f"Slave {position} is in {state_name(code)}. {why}; bring it to {wanted} with set_slave_state "
                "(needs ETHERCAT_STATE_CHANGE_ENABLED=true)"
            )

    def _require_cycling(self) -> None:
        if not self._mapped:
            raise EthercatMasterError(
                "The process image is not mapped yet. Process data exists once a slave has been brought to "
                "SAFEOP or OP with set_slave_state (needs ETHERCAT_STATE_CHANGE_ENABLED=true)"
            )
        if self._cycle_error:
            raise EthercatMasterError(f"The cyclic process-data exchange stopped: {self._cycle_error}")
        if self._cycle_thread is None or not self._cycle_thread.is_alive():
            raise EthercatMasterError("The cyclic process-data exchange is not running")

    def _layout(self, position: int, slave: Any) -> dict[str, Any] | None:
        """Process-data layout of one slave from its FMMU registers (cached), or None if unreadable."""
        if position in self._layouts:
            return self._layouts[position]
        layout: dict[str, Any] | None
        try:
            raw = bytes(slave._fprd(_FMMU_BASE, 16 * _FMMU_COUNT))
        except Exception as exc:  # private PySOEM API: be defensive
            logger.warning("Could not read the FMMU registers of slave %s: %s", position, exc)
            layout = None
        else:
            layout = {"outputs": [], "inputs": []}
            for n in range(_FMMU_COUNT):
                _, length, start_bit, end_bit, _, _, ftype, active = struct.unpack_from(_FMMU_FORMAT, raw, 16 * n)
                if active and ftype in (1, 2):
                    layout["outputs" if ftype == 2 else "inputs"].append((length, start_bit, end_bit))
        self._layouts[position] = layout
        return layout

    def _alignment(self, position: int, slave: Any, kind: str) -> tuple[int, int] | None:
        """``None`` if the slave's ``kind`` image is byte-aligned, else ``(first_bit, bit_count)``.

        Raises if the layout cannot be read or cannot be represented by PySOEM's
        one-byte view (bits spread over two bytes, or several bit FMMUs).
        """
        layout = self._layout(position, slave)
        direction = "outputs" if kind == "output" else "inputs"
        if layout is None:
            raise EthercatMasterError(
                f"Cannot tell whether slave {position}'s {kind} process data is byte-aligned (its FMMU registers "
                "could not be read), so the access is refused"
            )
        fmmus = layout[direction]
        if all(start == 0 and end == 7 for _, start, end in fmmus):
            return None
        if len(fmmus) == 1 and fmmus[0][0] == 1 and fmmus[0][1] <= fmmus[0][2]:
            _, start, end = fmmus[0]
            return start, end - start + 1
        raise EthercatMasterError(
            f"Slave {position}'s {kind} process data is bit-packed across byte boundaries ({fmmus}); "
            "this server cannot address it"
        )

    def _expected_wkc(self) -> int | None:
        if self.config.expected_wkc:
            return self.config.expected_wkc
        if self._master is None or not self._mapped:
            return None
        try:
            return int(self._master.expected_wkc)
        except Exception:  # pragma: no cover - defensive
            return None

    def _pd_meta(self) -> dict[str, Any]:
        return {"wkc": self._last_wkc, "expected_wkc": self._expected_wkc(), "cycle": self._cycle_count}

    # -- cyclic process data ------------------------------------------------

    def _start_cycle(self) -> None:
        if self._cycle_thread is not None and self._cycle_thread.is_alive():
            return
        self._cycle_stop.clear()
        self._cycle_error = None
        thread = threading.Thread(target=self._cycle_loop, name="ethercat-process-data", daemon=True)
        self._cycle_thread = thread
        thread.start()

    def _stop_cycle(self) -> None:
        thread = self._cycle_thread
        if thread is None:
            return
        self._cycle_stop.set()
        thread.join(timeout=5.0)
        self._cycle_thread = None
        with self._cycle_cv:
            self._cycle_cv.notify_all()

    def _cycle_loop(self) -> None:
        master = self._master
        period = self.config.cycle_time_us / 1_000_000
        rx_timeout_us = max(self.config.cycle_time_us, 2000)
        next_due = time.monotonic()
        while not self._cycle_stop.is_set():
            try:
                with self._pd_lock:
                    master.send_processdata(release_gil=True)
                    wkc = master.receive_processdata(timeout=rx_timeout_us, release_gil=True)
            except Exception as exc:
                logger.error("Process-data exchange failed: %s", exc)
                with self._cycle_cv:
                    self._cycle_error = f"{type(exc).__name__}: {exc}"
                    self._cycle_cv.notify_all()
                return
            with self._cycle_cv:
                self._last_wkc = int(wkc)
                self._cycle_count += 1
                self._cycle_cv.notify_all()
            next_due += period
            delay = next_due - time.monotonic()
            if delay > 0:
                self._cycle_stop.wait(delay)
            else:
                next_due = time.monotonic()

    def _wait_cycles(self, count: int) -> None:
        timeout = max(1.0, count * 10 * self.config.cycle_time_us / 1_000_000)
        with self._cycle_cv:
            target = self._cycle_count + count
            done = self._cycle_cv.wait_for(
                lambda: self._cycle_count >= target or self._cycle_error is not None or self._cycle_stop.is_set(),
                timeout=timeout,
            )
            if self._cycle_error is not None:
                raise EthercatMasterError(f"The cyclic process-data exchange stopped: {self._cycle_error}")
            if not done or self._cycle_count < target:
                raise EthercatMasterError("Timed out waiting for the process-data exchange")

    def _ensure_mapped(self) -> None:
        if self._mapped:
            return
        master = self._master
        # Map only. Without this, SOEM would request SAFE-OP for every slave.
        master.manual_state_change = 1
        try:
            size = master.config_map()
        except Exception as exc:
            raise EthercatMasterError(f"Mapping the process image failed: {exc}") from exc
        finally:
            master.manual_state_change = 0
        self._mapped = True
        self._io_map_size = int(size)
        self._start_cycle()

    # -- Backend API ----------------------------------------------------------

    @property
    def scanned(self) -> bool:
        return self._scanned

    def describe(self) -> dict[str, Any]:
        return {"backend": self.name, "interface": self.config.interface}

    def scan(self) -> list[dict[str, Any]]:
        with self._lock:
            master = self._ensure_open()
            if self._scanned:
                master.read_state()
                refuse_rescan([self._state_entry(i, slave) for i, slave in enumerate(self._slaves)])
            self._stop_cycle()
            self._layouts = {}
            self._mapped = False
            self._io_map_size = None
            self._scanned = False
            self._slaves = []
            master.manual_state_change = 0
            try:
                found = master.config_init(release_gil=True)
            except Exception as exc:
                raise EthercatMasterError(f"Bus scan failed: {exc}") from exc
            self._slaves = list(master.slaves or []) if found > 0 else []
            self._scanned = True
            return [self._describe(i, slave) for i, slave in enumerate(self._slaves)]

    def slave_count(self) -> int:
        return len(self._slaves)

    def slave_entry(self, position: int) -> dict[str, Any]:
        with self._lock:
            return self._describe(position, self._require_slave(position))

    def read_states(self) -> list[dict[str, Any]]:
        with self._lock:
            master = self._require_scanned()
            master.read_state()
            return [self._state_entry(i, slave) for i, slave in enumerate(self._slaves)]

    def read_pdo(self, position: int, offset: int, length: int | None) -> tuple[bytes, dict[str, Any]]:
        with self._lock:
            slave = self._require_slave(position)
            self._require_cycling()
            self._master.read_state()
            self._require_state(position, slave, (SAFEOP_STATE, OP_STATE), "Inputs are only valid in SAFEOP or OP")
        # Wait for one exchange after the state check, so the inputs are current.
        with self._lock:
            bits = self._alignment(position, slave, "input")
        self._wait_cycles(1)
        with self._pd_lock:
            image = bytes(slave.input)
        meta = self._pd_meta()
        if bits is not None and image:
            # Keep only this slave's bits of the shared byte, shifted down to bit 0.
            first, count = bits
            image = bytes([(image[0] >> first) & ((1 << count) - 1)])
            meta["bit_packed"] = {"first_bit": first, "bit_count": count}
        data = check_slice(image, offset, length, "input", position)
        meta["state"] = state_name(int(slave.state))
        return data, meta

    def write_pdo(self, position: int, offset: int, data: bytes) -> dict[str, Any]:
        with self._lock:
            slave = self._require_slave(position)
            self._require_cycling()
            self._master.read_state()
            self._require_state(position, slave, (OP_STATE,), "Outputs are only applied in OP")
            bits = self._alignment(position, slave, "output")
            if bits is not None:
                raise EthercatMasterError(
                    f"Slave {position}'s outputs are bits {bits[0]}-{bits[0] + bits[1] - 1} of a process-image byte "
                    "shared with other slaves; write_pdo writes whole bytes and would change their outputs, so it "
                    "is refused"
                )
            with self._pd_lock:
                image = bytearray(slave.output)
                check_slice(bytes(image), offset, len(data), "output", position)
                image[offset : offset + len(data)] = data
                slave.output = bytes(image)
        # Two exchanges: the next frame carries the new outputs, the one after confirms it.
        self._wait_cycles(2)
        meta = self._pd_meta()
        wkc = meta["wkc"]
        if not wkc or wkc <= 0:
            raise EthercatMasterError(
                f"The outputs were placed in the process image, but no slave answered the frame (working counter {wkc})"
            )
        if self.config.expected_wkc and wkc < self.config.expected_wkc:
            raise EthercatMasterError(
                f"The outputs were sent, but the working counter {wkc} is below ETHERCAT_EXPECTED_WKC="
                f"{self.config.expected_wkc}; the slave may not have taken them"
            )
        return meta

    def _sdo_error(self, action: str, position: int, index: int, subindex: int, exc: Exception) -> str:
        where = f"SDO {action} 0x{index:04X}:{subindex} on slave {position}"
        kind = type(exc).__name__
        if kind == "SdoError":
            code = int(getattr(exc, "abort_code", 0))
            return f"{where} aborted: 0x{code:08X} {getattr(exc, 'desc', '')}".rstrip()
        if kind == "WkcError":
            return (
                f"{where}: no answer (working counter {getattr(exc, 'wkc', None)}). CoE needs a slave with a "
                "mailbox in PREOP, SAFEOP or OP"
            )
        if kind in ("MailboxError", "PacketError"):
            code = getattr(exc, "error_code", None)
            return f"{where} failed: {kind} {code}: {getattr(exc, 'desc', '')}".rstrip()
        return f"{where} failed: {kind}: {exc}"

    def read_sdo(self, position: int, index: int, subindex: int) -> bytes:
        with self._lock:
            slave = self._require_slave(position)
            try:
                raw = slave.sdo_read(index, subindex, release_gil=True)
            except Exception as exc:
                raise EthercatMasterError(self._sdo_error("read", position, index, subindex, exc)) from exc
            return bytes(raw)

    def write_sdo(self, position: int, index: int, subindex: int, data: bytes) -> None:
        with self._lock:
            slave = self._require_slave(position)
            try:
                slave.sdo_write(index, subindex, bytes(data), release_gil=True)
            except Exception as exc:
                raise EthercatMasterError(self._sdo_error("write", position, index, subindex, exc)) from exc

    def set_state(self, position: int, state: int) -> dict[str, Any]:
        with self._lock:
            slave = self._require_slave(position)
            if state in (SAFEOP_STATE, OP_STATE):
                self._ensure_mapped()
                self._require_cycling()
            self._master.read_state()
            # A slave with its error indicator set ignores requests until the error is acknowledged.
            request = state | STATE_ACK if int(slave.state) & STATE_ERROR else state
            slave.state = request
            slave.write_state()
            deadline = time.monotonic() + self.config.timeout_us / 1_000_000
            while slave.state_check(state, _STATE_POLL_US) != state and time.monotonic() < deadline:
                time.sleep(_STATE_POLL_SLEEP_S)
            self._master.read_state()
            entry = self._state_entry(position, slave)
            if int(slave.state) != state:
                detail = f" (AL status {entry['al_status_code']}: {entry['al_status']})" if entry["al_status"] else ""
                raise EthercatMasterError(
                    f"Slave {position} did not reach {STATE_NAMES[state]}; it is in {entry['state']}{detail}"
                )
            return {"requested": STATE_NAMES[state], **entry}

    def status(self) -> dict[str, Any]:
        thread = self._cycle_thread
        return {
            "interface": self.config.interface,
            "cycle_time_us": self.config.cycle_time_us,
            "timeout_us": self.config.timeout_us,
            "interface_open": self._master is not None,
            "scanned": self._scanned,
            "slaves": len(self._slaves),
            "process_image_mapped": self._mapped,
            "io_map_size": self._io_map_size,
            "cyclic_exchange_running": bool(thread is not None and thread.is_alive()),
            "cycles": self._cycle_count,
            "last_wkc": self._last_wkc,
            "expected_wkc": self._expected_wkc(),
            "cycle_error": self._cycle_error,
        }

    def close(self) -> None:
        with self._lock:
            was_exchanging = self._mapped
            self._stop_cycle()
            master = self._master
            if master is None:
                return
            try:
                if was_exchanging:
                    # Leave the bus in a defined state, as PySOEM's examples do.
                    master.state = INIT_STATE | STATE_ACK
                    master.write_state()
            except Exception as exc:  # pragma: no cover - best effort on shutdown
                logger.warning("Could not request INIT before closing: %s", exc)
            finally:
                try:
                    master.close()
                finally:
                    self._master = None
                    self._slaves = []
                    self._scanned = False
                    self._mapped = False
                    self._io_map_size = None
                    self._layouts = {}


# ---------------------------------------------------------------------------
# Facade used by the MCP tools
# ---------------------------------------------------------------------------


def create_backend(config: EthercatConfig) -> Backend:
    if config.is_mock:
        from .mock_bridge import MockBridgeBackend

        return MockBridgeBackend(config)
    return PysoemBackend(config)


class EthercatMaster:
    """Async facade over a backend. Enforces the write and state-change gates."""

    def __init__(self, config: EthercatConfig, backend: Backend | None = None) -> None:
        self.config = config
        self.backend: Backend = backend or create_backend(config)

    async def _run(self, func: Callable[..., T], *args: Any) -> tuple[T, float]:
        start = time.perf_counter()
        result = await anyio.to_thread.run_sync(partial(func, *args))
        return result, round((time.perf_counter() - start) * 1000.0, 3)

    def describe(self) -> dict[str, Any]:
        return self.backend.describe()

    def require_writes(self) -> None:
        if not self.config.writes_enabled:
            raise GateError("Write operations are disabled (set ETHERCAT_WRITES_ENABLED=true to allow them)")

    def require_state_changes(self) -> None:
        if not self.config.state_change_enabled:
            raise GateError("State changes are disabled (set ETHERCAT_STATE_CHANGE_ENABLED=true to allow them)")

    async def scan_slaves(self) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        # Backends refuse a rescan while a slave is in SAFEOP/OP (checked under their own lock).
        devices, ms = await self._run(self.backend.scan)
        return devices, {"duration_ms": ms, "count": len(devices)}

    def _slave_sync(self, position: int) -> dict[str, Any]:
        if not self.backend.scanned:
            raise EthercatMasterError("No bus scan yet: call scan_network first")
        self.backend.read_states()  # refresh the AL state before reporting it
        return self.backend.slave_entry(position)

    async def get_slave(self, position: int) -> tuple[dict[str, Any], dict[str, Any]]:
        entry, ms = await self._run(self._slave_sync, position)
        return entry, {"duration_ms": ms}

    async def read_pdo(self, position: int, offset: int, length: int | None) -> tuple[bytes, dict[str, Any]]:
        (data, meta), ms = await self._run(self.backend.read_pdo, position, offset, length)
        return data, {**meta, "duration_ms": ms}

    async def write_pdo(self, position: int, offset: int, data: bytes) -> dict[str, Any]:
        self.require_writes()
        meta, ms = await self._run(self.backend.write_pdo, position, offset, data)
        return {**meta, "duration_ms": ms}

    async def read_sdo(self, position: int, index: int, subindex: int) -> tuple[bytes, dict[str, Any]]:
        raw, ms = await self._run(self.backend.read_sdo, position, index, subindex)
        return raw, {"duration_ms": ms}

    async def write_sdo(self, position: int, index: int, subindex: int, data: bytes) -> dict[str, Any]:
        self.require_writes()
        _, ms = await self._run(self.backend.write_sdo, position, index, subindex, data)
        return {"duration_ms": ms}

    async def set_slave_state(self, position: int, state: int) -> tuple[dict[str, Any], dict[str, Any]]:
        self.require_state_changes()
        result, ms = await self._run(self.backend.set_state, position, state)
        return result, {"duration_ms": ms}

    def _test_sync(self, position: int) -> dict[str, Any]:
        if not self.backend.scanned:
            raise EthercatMasterError("No bus scan yet: call scan_network first")
        states = self.backend.read_states()
        if not 0 <= position < len(states):
            raise EthercatMasterError(
                f"Slave position {position} is out of range (the last scan found {len(states)} slave(s))"
            )
        entry = states[position]
        if entry["state_code"] == NONE_STATE:
            raise EthercatMasterError(f"Slave {position} did not answer the AL state read")
        result: dict[str, Any] = {"slave_position": position, "responding": True, "state": entry["state"]}
        try:
            raw = self.backend.read_sdo(position, 0x1000, 0)
            result["coe"] = {"ok": True, "device_type": "0x" + raw[::-1].hex().upper() if raw else None}
        except EthercatMasterError as exc:
            result["coe"] = {"ok": False, "error": str(exc)}
        return result

    async def test_communication(self, position: int) -> tuple[dict[str, Any], dict[str, Any]]:
        result, ms = await self._run(self._test_sync, position)
        return result, {"duration_ms": ms}

    def connection_status(self) -> dict[str, Any]:
        status = {**self.describe(), **self.backend.status()}
        status["writes_enabled"] = self.config.writes_enabled
        status["state_change_enabled"] = self.config.state_change_enabled
        return status

    async def close(self) -> None:
        await anyio.to_thread.run_sync(self.backend.close)
