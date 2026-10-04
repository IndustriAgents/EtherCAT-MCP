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
from collections.abc import Callable, Iterator
from contextlib import contextmanager
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

# AL status codes with which a slave refuses a requested transition and stays where it was
# (ETG.1000.6). Runtime faults such as 0x0018/0x0019 (no valid inputs/outputs), 0x001A
# (synchronisation error) or 0x001B (sync-manager watchdog) can also appear after a slave
# reached the requested state, so they are not treated as refusals.
REFUSAL_AL_CODES = frozenset(
    [*range(0x0011, 0x0018), *range(0x001C, 0x0020), *range(0x0020, 0x002A), 0x0030, 0x0031, 0x0035, 0x0036, 0x0037]
)

# Extra time on top of EthercatConfig.call_deadline_s: config_init reads every slave's EEPROM,
# and the first SAFEOP/OP request maps the process image (CoE reads of the PDO assignment).
SCAN_EXTRA_S = 30.0
MAPPING_EXTRA_S = 20.0

# Broadcast AL-state reads before a scan: a lost frame reads as 0, so OR several.
_SEGMENT_STATE_READS = 3

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


# Outcome of a write, state change or other device-changing request (suite rule: no duplicate writes).
WRITTEN = "written"  # the device confirmed it
REJECTED = "rejected"  # the device answered and refused it
UNKNOWN = "unknown"  # it may have reached the device; read back before retrying
NOT_SENT = "not_sent"  # it never left the master


class EthercatMasterError(RuntimeError):
    """An EtherCAT operation failed. The message is shown to the client.

    For device-changing requests, ``outcome`` says whether it may have taken
    effect: WRITTEN, REJECTED, UNKNOWN or NOT_SENT (the default).
    """

    def __init__(self, message: str, *, outcome: str | None = None) -> None:
        super().__init__(message)
        self.outcome = outcome or NOT_SENT

    @property
    def request_sent(self) -> bool:
        return self.outcome != NOT_SENT


def unknown_outcome(message: str) -> EthercatMasterError:
    return EthercatMasterError(
        f"{message}. The request may have been applied; check/read back before retrying.", outcome=UNKNOWN
    )


# How often a call queued behind the backend lock re-checks whether it was cancelled.
_LOCK_POLL_S = 0.05


class CallToken:
    """One tool call's permission to send anything to the bus or the mock.

    Backends call :meth:`begin_send` immediately before every request that can change
    a device, and :meth:`check` after waiting for their lock and at other points.
    When the client cancels the call or disconnects, or the call's overall deadline
    passes, the facade calls :meth:`cancel`. From then on nothing more is sent: a call
    that never reached a send point ends ``not_sent`` and can never send later, even
    from a worker thread the facade has abandoned. ``begin_send`` and ``cancel``
    decide under one lock, so the outcome is never ambiguous.
    """

    def __init__(self, deadline_s: float | None = None) -> None:
        self._deadline = None if deadline_s is None else time.monotonic() + deadline_s
        self._lock = threading.Lock()
        self._reason: str | None = None
        self.sent = False

    def remaining(self) -> float | None:
        return None if self._deadline is None else self._deadline - time.monotonic()

    @property
    def cancelled(self) -> bool:
        return self._reason is not None

    def cancel(self, reason: str) -> bool:
        """Stop the call from sending anything more. Returns whether it already sent something."""
        with self._lock:
            if self._reason is None:
                self._reason = reason
            return self.sent

    def _stopped(self) -> bool:
        if self._reason is None and self._deadline is not None and time.monotonic() >= self._deadline:
            self._reason = "its overall deadline passed"
        return self._reason is not None

    def _error(self) -> EthercatMasterError:
        if self.sent:
            return unknown_outcome(f"The call stopped part way because {self._reason}")
        return EthercatMasterError(f"Not sent: {self._reason}. It will not be sent later.")

    def check(self) -> None:
        """Raise if the call was cancelled or its deadline passed."""
        with self._lock:
            if self._stopped():
                raise self._error()

    def begin_send(self) -> None:
        """Pass a send point: raise instead if the call may no longer send anything."""
        with self._lock:
            if self._stopped():
                raise self._error()
            self.sent = True


def _token(token: CallToken | None) -> CallToken:
    return token if token is not None else CallToken()


@contextmanager
def locked(lock: Any, token: CallToken) -> Iterator[None]:
    """Acquire ``lock`` while honouring ``token``: a cancelled or expired call never gets it."""
    while not lock.acquire(timeout=_LOCK_POLL_S):
        token.check()
    try:
        token.check()
        yield
    finally:
        lock.release()


class GateError(EthercatMasterError):
    """A write or state change was refused because its gate is disabled."""


class Backend(Protocol):
    """What :class:`EthercatMaster` needs from a backend. All calls are blocking."""

    name: str
    last_scan: dict[str, Any]

    @property
    def scanned(self) -> bool: ...

    def describe(self) -> dict[str, Any]: ...
    # Every call that may wait or touch the device takes the caller's CallToken.
    def scan(self, token: CallToken | None = None) -> list[dict[str, Any]]: ...
    def slave_count(self) -> int: ...
    def slave_entry(self, position: int, token: CallToken | None = None) -> dict[str, Any]: ...
    def read_states(self, token: CallToken | None = None) -> list[dict[str, Any]]: ...
    def read_pdo(
        self, position: int, offset: int, length: int | None, token: CallToken | None = None
    ) -> tuple[bytes, dict[str, Any]]: ...
    def write_pdo(self, position: int, offset: int, data: bytes, token: CallToken | None = None) -> dict[str, Any]: ...
    def read_sdo(self, position: int, index: int, subindex: int, token: CallToken | None = None) -> bytes: ...
    def write_sdo(
        self, position: int, index: int, subindex: int, data: bytes, token: CallToken | None = None
    ) -> None: ...
    def set_state(self, position: int, state: int, token: CallToken | None = None) -> dict[str, Any]: ...
    def status(self) -> dict[str, Any]: ...
    def close(self) -> None: ...


def refuse_live_segment(state_bits: int, state_changes_allowed: bool) -> None:
    """Refuse the first scan of a segment where a slave is in SAFEOP or OP.

    ``state_bits`` is the broadcast-read AL state (all slaves ORed together).
    A scan resets every slave to INIT, so on a live segment it is a state change.
    """
    if state_bits & (SAFEOP_STATE | OP_STATE) and not state_changes_allowed:
        raise EthercatMasterError(
            "Refusing to scan: at least one slave on the segment is in SAFEOP or OP (broadcast AL state "
            f"0x{state_bits:02X}), so another master may be running it, and a scan resets every slave to INIT. "
            "Set both ETHERCAT_WRITES_ENABLED=true and ETHERCAT_STATE_CHANGE_ENABLED=true to allow resetting a "
            "live segment."
        )


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

    * ``scan()`` first reads the AL state of the whole segment with one
      broadcast (``Master.state_check`` on slave 0) and refuses to go on if a
      slave is in SAFEOP or OP, unless both gates are on. Then it runs
      ``config_init``: SOEM resets every slave to INIT and requests PRE-OP.
    * The process image is mapped (``config_map``) only when a slave is first
      asked for SAFEOP or OP through ``set_state`` (the state-change gate).
      Mapping runs with ``manual_state_change`` set, so SOEM does not push
      every slave to SAFE-OP on its own. The output image is then zeroed (SOEM
      never clears it, so old bytes would otherwise be sent again), the
      process-data watchdog is set on every slave with outputs, and a
      background thread exchanges process data every ``ETHERCAT_CYCLE_TIME``.
    * A slave's outputs are zeroed in the image whenever it is asked to enter
      OP from a lower state and whenever it ends up outside OP, so re-entering
      OP never applies outputs left over from before.
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
        self._watchdog_errors: dict[int, str] = {}
        self.last_scan: dict[str, Any] = {}

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

    def _master_expected_wkc(self) -> int | None:
        if self._master is None or not self._mapped:
            return None
        try:
            return int(self._master.expected_wkc)
        except Exception:  # pragma: no cover - defensive
            return None

    def _expected_wkc(self) -> int | None:
        return self.config.expected_wkc or self._master_expected_wkc()

    def _pd_slaves_in(self, states: tuple[int, ...]) -> bool:
        """True if every slave with process data is in one of ``states`` without an error (after read_state)."""
        for slave in self._slaves:
            if not (len(slave.input) or len(slave.output)):
                continue
            code = int(slave.state)
            if code & STATE_ERROR or (code & 0x0F) not in states:
                return False
        return True

    def _wkc_note(self) -> str | None:
        """Say so when the working counter check is weaker because slaves share process-image bytes."""
        for position, slave in enumerate(self._slaves):
            for kind, view in (("input", slave.input), ("output", slave.output)):
                if not len(view):
                    continue
                try:
                    shared = self._alignment(position, slave, kind) is not None
                except EthercatMasterError:
                    shared = True  # layout unknown: assume the worst
                if shared:
                    return (
                        "Bit-packed slaves share process-image bytes. SOEM's expected working counter counts such a "
                        "byte once, so a slave that misses the frame can go unnoticed."
                    )
        return None

    def _clear_outputs(self, position: int, slave: Any) -> None:
        """Zero this slave's outputs in the process image (only its own bits if they are bit-packed)."""
        if not self._mapped or not len(slave.output):
            return
        try:
            bits = self._alignment(position, slave, "output")
        except EthercatMasterError:
            # Layout unknown or not addressable: write_pdo refuses such slaves, so the server
            # never put anything there since the image was zeroed after mapping.
            return
        with self._pd_lock:
            image = slave.output
            if bits is None:
                slave.output = bytes(len(image))
            else:
                first, count = bits
                mask = ((1 << count) - 1) << first
                slave.output = bytes([image[0] & ~mask & 0xFF]) + image[1:]

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
                    wkc = int(master.receive_processdata(timeout=rx_timeout_us, release_gil=True))
                    self._last_wkc = wkc  # same snapshot as the input image
            except Exception as exc:
                logger.error("Process-data exchange failed: %s", exc)
                with self._cycle_cv:
                    self._cycle_error = f"{type(exc).__name__}: {exc}"
                    self._cycle_cv.notify_all()
                return
            with self._cycle_cv:
                self._cycle_count += 1
                self._cycle_cv.notify_all()
            next_due += period
            delay = next_due - time.monotonic()
            if delay > 0:
                self._cycle_stop.wait(delay)
            else:
                next_due = time.monotonic()

    def _wait_cycles(self, count: int, token: CallToken | None = None) -> None:
        deadline = time.monotonic() + max(1.0, count * 10 * self.config.cycle_time_us / 1_000_000)
        with self._cycle_cv:
            target = self._cycle_count + count
            while not (self._cycle_count >= target or self._cycle_error is not None or self._cycle_stop.is_set()):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self._cycle_cv.wait(min(remaining, _LOCK_POLL_S))
                if token is not None:
                    token.check()
            if self._cycle_error is not None:
                raise EthercatMasterError(f"The cyclic process-data exchange stopped: {self._cycle_error}")
            if self._cycle_count < target:
                raise EthercatMasterError("Timed out waiting for the process-data exchange")

    def _ensure_mapped(self) -> bool:
        """Map the process image if needed. Returns True if it was mapped by this call."""
        if self._mapped:
            return False
        master = self._master
        # Map only. Without this, SOEM would request SAFE-OP for every slave.
        master.manual_state_change = 1
        try:
            size = master.config_map()
        except Exception as exc:
            # config_map writes sync-manager and FMMU registers as it goes.
            raise unknown_outcome(f"Mapping the process image failed part way: {exc}") from exc
        finally:
            master.manual_state_change = 0
        self._mapped = True
        self._io_map_size = int(size)
        # SOEM never clears the image: after a rescan, old bytes would go to whichever slave now
        # sits at that offset. Start from zero before the first frame is sent.
        with self._pd_lock:
            for slave in self._slaves:
                if len(slave.output):
                    slave.output = bytes(len(slave.output))
        self._set_watchdogs()
        self._start_cycle()
        return True

    def _set_watchdogs(self) -> None:
        """Set the process-data watchdog of every slave with outputs (PySOEM's set_watchdog)."""
        self._watchdog_errors = {}
        for position, slave in enumerate(self._slaves):
            if not len(slave.output):
                continue
            try:
                slave.set_watchdog("processdata", self.config.pd_watchdog_ms)
            except Exception as exc:
                message = str(exc) or type(exc).__name__
                self._watchdog_errors[position] = message
                logger.warning(
                    "Could not set the process-data watchdog of slave %s (its ESC default applies): %s",
                    position,
                    message,
                )

    # -- Backend API ----------------------------------------------------------

    @property
    def scanned(self) -> bool:
        return self._scanned

    def describe(self) -> dict[str, Any]:
        return {"backend": self.name, "interface": self.config.interface}

    def scan(self, token: CallToken | None = None) -> list[dict[str, Any]]:
        token = _token(token)
        with locked(self._lock, token):
            master = self._ensure_open()
            if self._scanned:
                master.read_state()
                refuse_rescan([self._state_entry(i, slave) for i, slave in enumerate(self._slaves)])
            # Broadcast reads of the AL state: every slave's state ORed together. A lost frame
            # reads as 0, so several reads are ORed before deciding the segment is idle.
            bits = 0
            for _ in range(_SEGMENT_STATE_READS):
                bits |= int(master.state_check(INIT_STATE, 2000))
            refuse_live_segment(bits, self.config.state_changes_allowed)
            self.last_scan = {
                "segment_state_before": state_name(bits) if bits else "NONE (no answer)",
                "reset_live_segment": bool(bits & (SAFEOP_STATE | OP_STATE)),
            }
            # Send point: stopping process data and config_init change what the slaves see.
            token.begin_send()
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
                # config_init resets slaves as it goes: whatever happened may have taken effect.
                raise unknown_outcome(f"Bus scan failed: {exc}") from exc
            self._slaves = list(master.slaves or []) if found > 0 else []
            self._scanned = True
            return [self._describe(i, slave) for i, slave in enumerate(self._slaves)]

    def slave_count(self) -> int:
        return len(self._slaves)

    def slave_entry(self, position: int, token: CallToken | None = None) -> dict[str, Any]:
        with locked(self._lock, _token(token)):
            return self._describe(position, self._require_slave(position))

    def read_states(self, token: CallToken | None = None) -> list[dict[str, Any]]:
        with locked(self._lock, _token(token)):
            master = self._require_scanned()
            master.read_state()
            return [self._state_entry(i, slave) for i, slave in enumerate(self._slaves)]

    def read_pdo(
        self, position: int, offset: int, length: int | None, token: CallToken | None = None
    ) -> tuple[bytes, dict[str, Any]]:
        token = _token(token)
        with locked(self._lock, token):
            slave = self._require_slave(position)
            self._require_cycling()
            self._master.read_state()
            self._require_state(position, slave, (SAFEOP_STATE, OP_STATE), "Inputs are only valid in SAFEOP or OP")
            bits = self._alignment(position, slave, "input")
            all_exchanging = self._pd_slaves_in((SAFEOP_STATE, OP_STATE))
        # Wait for one exchange after the state check, so the inputs are current.
        self._wait_cycles(1, token)
        with self._pd_lock:
            image = bytes(slave.input)
            wkc = self._last_wkc
        expected = self.config.expected_wkc or (self._master_expected_wkc() if all_exchanging else None)
        if wkc is None or wkc <= 0:
            raise EthercatMasterError(
                f"No slave answered the last process-data frame (working counter {wkc}); the inputs are stale"
            )
        if expected and wkc < expected:
            raise EthercatMasterError(
                f"The working counter {wkc} is below the expected {expected}: at least one slave did not process "
                "the frame, so the inputs may be stale"
            )
        meta = {"wkc": wkc, "expected_wkc": expected, "cycle": self._cycle_count}
        with locked(self._lock, token):
            note = self._wkc_note()
        if note:
            meta["wkc_note"] = note
        if bits is not None and image:
            # Keep only this slave's bits of the shared byte, shifted down to bit 0.
            first, count = bits
            image = bytes([(image[0] >> first) & ((1 << count) - 1)])
            meta["bit_packed"] = {"first_bit": first, "bit_count": count}
        data = check_slice(image, offset, length, "input", position)
        meta["state"] = state_name(int(slave.state))
        return data, meta

    def write_pdo(self, position: int, offset: int, data: bytes, token: CallToken | None = None) -> dict[str, Any]:
        token = _token(token)
        with locked(self._lock, token):
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
                token.begin_send()  # send point: the next frame carries these outputs
                slave.output = bytes(image)
        # From here on the outputs are in the image and go out with every frame: never resend,
        # and whatever goes wrong while confirming them is "unknown", never "not sent".
        try:
            return self._confirm_outputs(position, slave, token)
        except EthercatMasterError as exc:
            if exc.outcome == UNKNOWN:
                raise
            raise unknown_outcome(f"The outputs are in the process image, but {exc}") from exc
        except Exception as exc:
            raise unknown_outcome(
                f"The outputs are in the process image, but confirming them failed: {type(exc).__name__}: {exc}"
            ) from exc

    def _confirm_outputs(self, position: int, slave: Any, token: CallToken) -> dict[str, Any]:
        # Two exchanges: the first carries the new outputs, the second is fully after them.
        self._wait_cycles(2, token)
        with self._pd_lock:
            wkc = self._last_wkc
        with locked(self._lock, token):
            self._master.read_state()
            target_ok = int(slave.state) == OP_STATE
            all_op = self._pd_slaves_in((OP_STATE,))
            note = self._wkc_note()
        if self.config.expected_wkc:
            expected, verified = self.config.expected_wkc, "configured_wkc"
        elif all_op and self._master_expected_wkc():
            expected, verified = self._master_expected_wkc(), "expected_wkc"
        else:
            # Not every process-data slave is in OP, so the group's count cannot be expected:
            # only "some slave answered" and "the target is still in OP" can be checked.
            expected, verified = None, "slave_state_only"
        meta: dict[str, Any] = {"wkc": wkc, "expected_wkc": expected, "cycle": self._cycle_count}
        if note:
            meta["wkc_note"] = note
        if wkc is None or wkc <= 0:
            raise unknown_outcome(
                f"The outputs are in the process image, but no slave answered the frame (working counter {wkc})"
            )
        if expected and wkc < expected:
            raise unknown_outcome(
                f"The outputs were sent, but the working counter {wkc} is below the expected {expected}, "
                "so not every slave processed the frame"
            )
        if not target_ok:
            raise unknown_outcome(
                f"The outputs were sent, but slave {position} is now in {state_name(int(slave.state))}, not OP"
            )
        meta["verified"] = verified
        meta["outcome"] = WRITTEN
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

    def read_sdo(self, position: int, index: int, subindex: int, token: CallToken | None = None) -> bytes:
        with locked(self._lock, _token(token)):
            slave = self._require_slave(position)
            try:
                raw = slave.sdo_read(index, subindex, release_gil=True)
            except Exception as exc:
                raise EthercatMasterError(self._sdo_error("read", position, index, subindex, exc)) from exc
            return bytes(raw)

    def write_sdo(self, position: int, index: int, subindex: int, data: bytes, token: CallToken | None = None) -> None:
        token = _token(token)
        with locked(self._lock, token):
            slave = self._require_slave(position)
            payload = bytes(data)
            token.begin_send()  # send point: the download goes out now
            try:
                slave.sdo_write(index, subindex, payload, release_gil=True)
            except Exception as exc:
                message = self._sdo_error("write", position, index, subindex, exc)
                kind = type(exc).__name__
                if kind == "NetworkInterfaceNotOpenError":
                    raise EthercatMasterError(message) from exc  # raised before anything was sent
                if kind == "SdoError" and self._abort_refused_whole_request(exc, position, index, subindex, data):
                    raise EthercatMasterError(message, outcome=REJECTED) from exc
                # A segmented download aborted part way, an error that SOEM's shared error ring
                # holds for some other request, a mailbox or packet error, or no answer at all:
                # the download may have been applied. Never repeat it.
                raise unknown_outcome(message) from exc

    @staticmethod
    def _abort_refused_whole_request(exc: Exception, position: int, index: int, subindex: int, data: bytes) -> bool:
        """True only if this abort is for this request and nothing of it can have been applied.

        PySOEM raises the first error in SOEM's shared error ring, which may belong to an
        earlier request, so slave (1-based in SOEM), index and subindex must match. Only an
        expedited download (at most 4 bytes, one mailbox frame) is all-or-nothing; longer
        downloads may be segmented and aborted after some segments were taken.
        """
        return (
            len(data) <= 4
            and getattr(exc, "slave_pos", None) == position + 1
            and getattr(exc, "index", None) == index
            and getattr(exc, "subindex", None) == subindex
        )

    def set_state(self, position: int, state: int, token: CallToken | None = None) -> dict[str, Any]:
        token = _token(token)
        with locked(self._lock, token):
            slave = self._require_slave(position)
            mapped_now = False
            if state in (SAFEOP_STATE, OP_STATE):
                if not self._mapped:
                    token.begin_send()  # send point: mapping writes sync-manager, FMMU and watchdog registers
                    mapped_now = self._ensure_mapped()
                self._require_cycling()
            self._master.read_state()
            current = int(slave.state)
            al_before = int(slave.al_status)
            if state == OP_STATE and current != OP_STATE:
                # Enter OP with zero outputs, never with bytes left in the image from before, and
                # let two frames carry the zeros into the slave's sync manager before asking for OP.
                token.check()
                self._clear_outputs(position, slave)
                self._wait_cycles(2, token)
            # A slave with its error indicator set ignores requests until the error is acknowledged.
            slave.state = state | STATE_ACK if current & STATE_ERROR else state
            sent_before = token.sent
            token.begin_send()  # send point: the AL control write
            try:
                slave.write_state()
            except Exception as exc:
                if type(exc).__name__ == "NetworkInterfaceNotOpenError" and not sent_before:
                    raise EthercatMasterError(f"Requesting {STATE_NAMES[state]} failed: {exc}") from exc
                raise unknown_outcome(f"Requesting {STATE_NAMES[state]} for slave {position} failed: {exc}") from exc
            # The request may have reached the slave: from here on, failures are "unknown".
            try:
                deadline = time.monotonic() + self.config.timeout_us / 1_000_000
                while slave.state_check(state, _STATE_POLL_US) != state and time.monotonic() < deadline:
                    token.check()  # raises "unknown" (the request went out) if the call was cancelled
                    time.sleep(_STATE_POLL_SLEEP_S)
                self._master.read_state()
                final = int(slave.state)
                al_after = int(slave.al_status)
                if final != OP_STATE:
                    # Outside OP the slave does not apply outputs; clear its part of the image so
                    # that entering OP later does not re-apply stale bytes.
                    self._clear_outputs(position, slave)
                entry = self._state_entry(position, slave)
            except EthercatMasterError as exc:
                if exc.outcome == UNKNOWN:
                    raise
                raise unknown_outcome(
                    f"Requested {STATE_NAMES[state]} for slave {position}, but checking the result failed: {exc}"
                ) from exc
            except Exception as exc:
                raise unknown_outcome(
                    f"Requested {STATE_NAMES[state]} for slave {position}, but checking the result failed: {exc}"
                ) from exc
            result: dict[str, Any] = {"requested": STATE_NAMES[state], **entry}
            if mapped_now:
                result["process_image_mapped"] = True
                result["pd_watchdog_errors"] = {str(k): v for k, v in self._watchdog_errors.items()}
            if final != state:
                detail = f" (AL status {entry['al_status_code']}: {entry['al_status']})" if entry["al_status"] else ""
                message = f"Slave {position} did not reach {STATE_NAMES[state]}; it is in {entry['state']}{detail}"
                refused = (
                    final & STATE_ERROR
                    and (final & 0x0F) == (current & 0x0F)  # still where it started
                    and al_after in REFUSAL_AL_CODES
                    and (not current & STATE_ERROR or al_after != al_before)  # not a leftover code
                )
                if refused:
                    raise EthercatMasterError(message, outcome=REJECTED)
                raise unknown_outcome(f"{message} after {self.config.timeout_us} us")
            result["outcome"] = WRITTEN
            return result

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
            "pd_watchdog_ms": self.config.pd_watchdog_ms,
            "pd_watchdog_errors": {str(k): v for k, v in self._watchdog_errors.items()},
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
    """Async facade over a backend. Enforces the gates, deadlines and cancellation."""

    def __init__(self, config: EthercatConfig, backend: Backend | None = None) -> None:
        self.config = config
        self.backend: Backend = backend or create_backend(config)
        self._pending: set[CallToken] = set()
        self._closing: str | None = None

    def shutdown_calls(self, reason: str) -> None:
        """Stop every pending call from sending anything more, and refuse new ones.

        Called when the MCP client disconnects (stdin closed) and when the server shuts
        down: FastMCP lets in-flight handlers finish, and without this a write still
        queued behind the backend lock would go out after the client is gone.
        """
        self._closing = reason
        for token in list(self._pending):
            token.cancel(reason)

    async def _run(
        self, func: Callable[..., T], *args: Any, write: bool = False, extra_s: float = 0.0
    ) -> tuple[T, float]:
        """Run a blocking backend call in a worker thread, bounded by an overall deadline.

        The call gets a CallToken. Whatever ends the wait without a result (the
        deadline, the client cancelling, the client disconnecting) cancels the token
        first, so the abandoned worker thread can never send anything afterwards
        (suite rules 1 and 12). A write that had not passed a send point is
        ``not_sent``; one that had is ``unknown``.
        """
        what = getattr(func, "__name__", "request")
        if self._closing is not None:
            raise EthercatMasterError(f"Not sent: {self._closing}.")
        deadline = self.config.call_deadline_s + extra_s
        token = CallToken(deadline)
        self._pending.add(token)
        start = time.perf_counter()
        try:
            with anyio.fail_after(deadline):
                result = await anyio.to_thread.run_sync(partial(func, *args, token=token), abandon_on_cancel=True)
        except TimeoutError:
            sent = token.cancel(f"its overall deadline of {deadline:.0f} s passed")
            if write and sent:
                raise unknown_outcome(f"The {what} request did not finish within {deadline:.0f} s") from None
            suffix = "; it was not sent and will not be sent later" if write else ""
            raise EthercatMasterError(f"The {what} request did not finish within {deadline:.0f} s{suffix}") from None
        except BaseException:
            token.cancel("the call was cancelled")
            raise
        finally:
            self._pending.discard(token)
        return result, round((time.perf_counter() - start) * 1000.0, 3)

    def describe(self) -> dict[str, Any]:
        return self.backend.describe()

    def require_writes(self) -> None:
        if not self.config.writes_enabled:
            raise GateError("Write operations are disabled (set ETHERCAT_WRITES_ENABLED=true to allow them)")

    def require_state_changes(self) -> None:
        # Suite rule: a separate gate never replaces the write gate; both must be on.
        if not self.config.state_changes_allowed:
            raise GateError(
                "State changes are disabled (they need both ETHERCAT_WRITES_ENABLED=true and "
                "ETHERCAT_STATE_CHANGE_ENABLED=true)"
            )

    async def scan_slaves(self) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        # Backends refuse a rescan while a slave is in SAFEOP/OP (checked under their own lock).
        devices, ms = await self._run(self.backend.scan, write=True, extra_s=SCAN_EXTRA_S)
        return devices, {"duration_ms": ms, "count": len(devices), **self.backend.last_scan}

    def _slave_sync(self, position: int, token: CallToken | None = None) -> dict[str, Any]:
        if not self.backend.scanned:
            raise EthercatMasterError("No bus scan yet: call scan_network first")
        self.backend.read_states(token=token)  # refresh the AL state before reporting it
        return self.backend.slave_entry(position, token=token)

    async def get_slave(self, position: int) -> tuple[dict[str, Any], dict[str, Any]]:
        entry, ms = await self._run(self._slave_sync, position)
        return entry, {"duration_ms": ms}

    async def read_pdo(self, position: int, offset: int, length: int | None) -> tuple[bytes, dict[str, Any]]:
        (data, meta), ms = await self._run(self.backend.read_pdo, position, offset, length)
        return data, {**meta, "duration_ms": ms}

    async def write_pdo(self, position: int, offset: int, data: bytes) -> dict[str, Any]:
        self.require_writes()
        meta, ms = await self._run(self.backend.write_pdo, position, offset, data, write=True)
        return {**meta, "duration_ms": ms}

    async def read_sdo(self, position: int, index: int, subindex: int) -> tuple[bytes, dict[str, Any]]:
        raw, ms = await self._run(self.backend.read_sdo, position, index, subindex)
        return raw, {"duration_ms": ms}

    async def write_sdo(self, position: int, index: int, subindex: int, data: bytes) -> dict[str, Any]:
        self.require_writes()
        _, ms = await self._run(self.backend.write_sdo, position, index, subindex, data, write=True)
        return {"duration_ms": ms, "outcome": WRITTEN}

    async def set_slave_state(self, position: int, state: int) -> tuple[dict[str, Any], dict[str, Any]]:
        self.require_state_changes()
        result, ms = await self._run(self.backend.set_state, position, state, write=True, extra_s=MAPPING_EXTRA_S)
        return result, {"duration_ms": ms}

    def _test_sync(self, position: int, token: CallToken | None = None) -> dict[str, Any]:
        if not self.backend.scanned:
            raise EthercatMasterError("No bus scan yet: call scan_network first")
        states = self.backend.read_states(token=token)
        if not 0 <= position < len(states):
            raise EthercatMasterError(
                f"Slave position {position} is out of range (the last scan found {len(states)} slave(s))"
            )
        entry = states[position]
        if entry["state_code"] == NONE_STATE:
            raise EthercatMasterError(f"Slave {position} did not answer the AL state read")
        result: dict[str, Any] = {"slave_position": position, "responding": True, "state": entry["state"]}
        try:
            raw = self.backend.read_sdo(position, 0x1000, 0, token=token)
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
        status["state_changes_allowed"] = self.config.state_changes_allowed
        return status

    async def close(self) -> None:
        """Stop pending calls, then close the backend (bounded: a device call stuck in a
        worker thread holds the backend lock, and shutdown must not wait for it forever)."""
        self.shutdown_calls("the server is shutting down")
        try:
            with anyio.fail_after(self.config.call_deadline_s):
                await anyio.to_thread.run_sync(self.backend.close, abandon_on_cancel=True)
        except TimeoutError:
            logger.warning("Closing the EtherCAT master did not finish within %.0f s", self.config.call_deadline_s)
