"""In-memory stand-in for the parts of PySOEM 1.1.12 that PysoemBackend uses.

Real EtherCAT needs raw Ethernet, root and hardware, so the PySOEM code paths
are tested against this fake. It mirrors PySOEM's names and signatures and the
SOEM behaviour the backend depends on:

* Slaves are physical devices on a bus (``FakeDevice``) that keep their AL
  state across scans. ``config_init`` resets them to INIT, then requests PRE-OP
  unless ``manual_state_change`` is set; ``config_map`` requests SAFE-OP unless
  it is set. Each ``config_init`` creates new ``CdefSlave``-like views.
* The process image lives in the Master (PySOEM's ``io_map``) and is never
  cleared by SOEM: not by ``config_init``, not by ``config_map``. Devices in OP
  apply whatever the image holds; leaving OP drops their applied outputs to 0.
* ``receive_processdata`` returns the working counter, or -1 (EC_NOFRAME)
  without touching the inputs when the frame is lost. Inputs and outputs count
  1 and 2 per device in SAFEOP/OP, like ``Master.expected_wkc``.
* ``state_check`` returns ``rval & 0x0f`` and stores the full AL status
  (including the error bit) in ``state``. On the Master (slave 0) it is a
  broadcast read: the states of all devices ORed together.
* A device with its error indicator set ignores requests without the
  acknowledge bit. Invalid transitions set the error bit and AL status 0x0011.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field

NONE_STATE = 0x00
INIT_STATE = 0x01
PREOP_STATE = 0x02
BOOT_STATE = 0x03
SAFEOP_STATE = 0x04
OP_STATE = 0x08
STATE_ACK = 0x10
STATE_ERROR = 0x10

EC_NOFRAME = -1

VALID = {
    (INIT_STATE, PREOP_STATE),
    (PREOP_STATE, SAFEOP_STATE),
    (SAFEOP_STATE, OP_STATE),
    (PREOP_STATE, INIT_STATE),
    (SAFEOP_STATE, INIT_STATE),
    (OP_STATE, INIT_STATE),
    (SAFEOP_STATE, PREOP_STATE),
    (OP_STATE, PREOP_STATE),
    (OP_STATE, SAFEOP_STATE),
}


class SdoError(Exception):
    def __init__(self, slave_pos, index, subindex, abort_code, desc):
        self.slave_pos = slave_pos
        self.index = index
        self.subindex = subindex
        self.abort_code = abort_code
        self.desc = desc


class WkcError(Exception):
    def __init__(self, message=None, wkc=None):
        self.message = message
        self.wkc = wkc


class MailboxError(Exception):
    def __init__(self, slave_pos, error_code, desc):
        self.slave_pos = slave_pos
        self.error_code = error_code
        self.desc = desc


class PacketError(Exception):
    def __init__(self, slave_pos, error_code):
        self.slave_pos = slave_pos
        self.error_code = error_code

    @property
    def desc(self):
        return "packet error"


class ConfigMapError(Exception):
    def __init__(self, error_list):
        self.error_list = error_list


class NetworkInterfaceNotOpenError(Exception):
    """Error when a master or slave method is used and the context has not been initialized."""


def al_status_code_to_string(code):
    return {0x0000: "No error", 0x0011: "Invalid requested state change"}.get(code, "Unknown")


@dataclass
class SlaveSpec:
    name: str
    man: int
    id: int
    rev: int
    ibytes: int = 0
    obytes: int = 0
    mailbox: bool = False
    objects: dict = field(default_factory=dict)  # (index, subindex) -> [bytes, writable]
    # FMMUs SOEM would program: (type 1=inputs/2=outputs, length, start bit, end bit). None: byte-aligned.
    fmmus: list | None = None
    # Byte offsets in the process image; None: allocated in order (bit-packed slaves share bytes).
    out_offset: int | None = None
    in_offset: int | None = None
    fixed_input: bytes | None = None  # input bytes the device delivers (None: simulated values)
    fprd_fails: bool = False
    sdo_reply_lost: bool = False  # SDO downloads are applied, but the answer never arrives (WkcError)
    watchdog_fails: bool = False


def default_bus() -> list[SlaveSpec]:
    return [
        SlaveSpec("EK1100", 2, 0x044C2C52, 0x00110000),
        SlaveSpec("EL1008", 2, 0x03F03052, 0x00120000, ibytes=1),
        SlaveSpec("EL2008", 2, 0x07D83052, 0x00120000, obytes=1),
        SlaveSpec(
            "EL3102",
            2,
            0x0C1E3052,
            0x00140000,
            ibytes=4,
            mailbox=True,
            objects={
                (0x1000, 0): [b"\x89\x13\x01\x01", False],
                (0x1018, 1): [b"\x02\x00\x00\x00", False],
                (0x8000, 6): [b"\x00", True],
            },
        ),
    ]


class FakeDevice:
    """A physical slave. Its state survives scans, like real hardware."""

    def __init__(self, bus: FakeBus, index: int, spec: SlaveSpec, state: int) -> None:
        self.bus = bus
        self.index = index
        self.spec = spec
        self.actual = state
        self.al_status = 0
        self.applied_outputs = bytes(spec.obytes)
        self.objects = {k: list(v) for k, v in spec.objects.items()}
        self.watchdog: dict[str, float] = {}

    def request(self, requested: int) -> None:
        target = requested & 0x0F
        if self.actual & STATE_ERROR:
            if not requested & STATE_ACK:
                return  # an ESC with its error indicator set waits for an acknowledge
            self.actual &= 0x0F
            self.al_status = 0
        if target == self.actual:
            self.al_status = 0
            return
        if (self.actual, target) not in VALID or (target in (SAFEOP_STATE, OP_STATE) and not self.bus.mapped):
            self.actual |= STATE_ERROR
            self.al_status = 0x0011
            return
        if self.actual == OP_STATE:
            self.applied_outputs = bytes(self.spec.obytes)  # outputs drop to their safe state
        self.actual = target
        self.al_status = 0

    @property
    def exchanging(self) -> bool:
        return (self.actual & 0x0F) in (SAFEOP_STATE, OP_STATE) and not self.actual & STATE_ERROR


class FakeBus:
    def __init__(self, specs: list[SlaveSpec], power_on_state: int = INIT_STATE) -> None:
        self.devices = [FakeDevice(self, i, spec, power_on_state) for i, spec in enumerate(specs)]
        self.mapped = False
        self.calls: list[tuple] = []
        self.lose_frames = False  # receive_processdata returns EC_NOFRAME
        self.mute: set[int] = set()  # devices that do not process the frame (no WKC, no data)
        self.fail_receive: Exception | None = None
        self.drop_after_write: int | None = None  # device that falls to SAFEOP+ERROR once its outputs change
        self.cycles = 0


class FakeSlave:
    """What ``Master.slaves`` holds after ``config_init`` (PySOEM's CdefSlave)."""

    def __init__(self, master: FakeMaster, pos: int, device: FakeDevice) -> None:
        self._master = master
        self._pos = pos
        self._dev = device
        spec = device.spec
        self.name = spec.name
        self.man = spec.man
        self.id = spec.id
        self.rev = spec.rev
        self.state = device.actual  # context slavelist state: last read state, and the request for write_state
        self.out_offset: int | None = None
        self.in_offset: int | None = None
        self.sdo_calls: list[tuple] = []

    @property
    def al_status(self) -> int:
        return self._dev.al_status

    # process image views into the Master's io_map (empty until config_map)
    @property
    def input(self) -> bytes:
        if self.in_offset is None:
            return b""
        size = max(self._dev.spec.ibytes, 1)
        return bytes(self._master.io_map[self.in_offset : self.in_offset + size])

    @property
    def output(self) -> bytes:
        if self.out_offset is None:
            return b""
        size = max(self._dev.spec.obytes, 1)
        return bytes(self._master.io_map[self.out_offset : self.out_offset + size])

    @output.setter
    def output(self, value: bytes) -> None:
        assert isinstance(value, bytes)
        assert self.out_offset is not None
        self._master.io_map[self.out_offset : self.out_offset + len(value)] = value  # memcpy, like PySOEM

    def write_state(self):
        self._master._check()
        self._master._bus.calls.append(("slave.write_state", self._pos, self.state))
        self._dev.request(self.state)
        return 1

    def state_check(self, expected_state, timeout=2000):
        self._master._check()
        self._master._bus.calls.append(("slave.state_check", self._pos, expected_state, timeout))
        self.state = self._dev.actual
        return self._dev.actual & 0x0F

    def _mailbox(self) -> None:
        self._master._check()
        if not self._dev.spec.mailbox or (self._dev.actual & 0x0F) in (NONE_STATE, INIT_STATE):
            raise WkcError(wkc=0)

    def sdo_read(self, index, subindex, size=0, ca=False, *, release_gil=None):
        self.sdo_calls.append(("read", index, subindex, release_gil))
        self._mailbox()
        if (index, subindex) not in self._dev.objects:
            raise SdoError(self._pos + 1, index, subindex, 0x06020000, "Object does not exist")
        return bytes(self._dev.objects[(index, subindex)][0])

    def sdo_write(self, index, subindex, data, ca=False, *, release_gil=None):
        assert isinstance(data, bytes)
        self.sdo_calls.append(("write", index, subindex, data, release_gil))
        self._mailbox()
        if (index, subindex) not in self._dev.objects:
            raise SdoError(self._pos + 1, index, subindex, 0x06020000, "Object does not exist")
        entry = self._dev.objects[(index, subindex)]
        if not entry[1]:
            raise SdoError(self._pos + 1, index, subindex, 0x06010002, "Attempt to write a read only object")
        entry[0] = data
        if self._dev.spec.sdo_reply_lost:
            raise WkcError(wkc=0)

    def set_watchdog(self, wd_type, wd_time_ms):
        self._master._check()
        if wd_type not in ("pdi", "processdata"):
            raise AttributeError()
        if self._dev.spec.watchdog_fails:
            raise WkcError()
        self._dev.watchdog[wd_type] = wd_time_ms
        self._master._bus.calls.append(("slave.set_watchdog", self._pos, wd_type, wd_time_ms))

    def _fprd(self, address, size, timeout_us=2000):
        """FMMU registers as SOEM's config_map would have programmed them."""
        self._master._check()
        if self._dev.spec.fprd_fails:
            raise WkcError()
        regs = bytearray(size)
        if address == 0x0600 and self._master._bus.mapped:
            spec = self._dev.spec
            fmmus = spec.fmmus
            if fmmus is None:
                fmmus = [(2, spec.obytes, 0, 7)] if spec.obytes else []
                fmmus += [(1, spec.ibytes, 0, 7)] if spec.ibytes else []
            for n, (ftype, length, start, end) in enumerate(fmmus):
                if 16 * (n + 1) <= size:
                    struct.pack_into("<IHBBHBBB", regs, 16 * n, 0, length, start, end, 0x1000, 0, ftype, 1)
        return bytes(regs)


class FakeMaster:
    def __init__(self, pysoem: FakePysoem) -> None:
        self._pysoem = pysoem
        self._bus = pysoem.bus
        self.io_map = bytearray(4096)  # lives as long as the Master, like PySOEM's io_map
        self.slaves = None
        self.manual_state_change = 0
        self.state = NONE_STATE
        self.context_initialized = False

    def open(self, ifname, ifname_red=None):
        self._bus.calls.append(("open", ifname))
        if ifname in self._pysoem.missing_interfaces:
            raise ConnectionError(f"could not open interface {ifname}")
        self.context_initialized = True

    def _check(self):
        if not self.context_initialized:
            raise NetworkInterfaceNotOpenError(
                "SOEM Network interface is not initialized or has been closed. Call Master.open() first"
            )

    def config_init(self, usetable=False, *, release_gil=None):
        self._check()
        self._bus.calls.append(("config_init", self.manual_state_change, release_gil))
        self._bus.mapped = False
        self.slaves = []
        for dev in self._bus.devices:
            if dev.actual == OP_STATE:
                dev.applied_outputs = bytes(dev.spec.obytes)
            dev.actual = INIT_STATE  # SOEM resets every slave to INIT (broadcast, with ACK)
            dev.al_status = 0
            if not self.manual_state_change:
                dev.actual = PREOP_STATE
        self.slaves = [FakeSlave(self, i, dev) for i, dev in enumerate(self._bus.devices)]
        return len(self.slaves) if self.slaves else -1

    def config_map(self):
        self._check()
        self._bus.calls.append(("config_map", self.manual_state_change))
        # SOEM lays out all outputs first, then all inputs. It does not clear io_map.
        offset = 0
        for slave in self.slaves:
            spec = slave._dev.spec
            if spec.obytes:
                slave.out_offset = spec.out_offset if spec.out_offset is not None else offset
                offset = max(offset, slave.out_offset + spec.obytes)
        for slave in self.slaves:
            spec = slave._dev.spec
            if spec.ibytes:
                slave.in_offset = spec.in_offset if spec.in_offset is not None else offset
                offset = max(offset, slave.in_offset + spec.ibytes)
        self._bus.mapped = True
        if not self.manual_state_change:
            for slave in self.slaves:
                slave._dev.request(SAFEOP_STATE)
        return offset

    def read_state(self):
        self._check()
        for slave in self.slaves or []:
            slave.state = slave._dev.actual
        return min((s._dev.actual & 0x0F for s in self.slaves or []), default=NONE_STATE)

    def write_state(self):
        self._check()
        self._bus.calls.append(("master.write_state", self.state))
        for slave in self.slaves or []:
            slave._dev.request(self.state)
        return len(self.slaves or [])

    def state_check(self, expected_state, timeout=50000):
        """Slave 0: broadcast read of the AL status, all slaves' states ORed together."""
        self._check()
        self._bus.calls.append(("master.state_check", expected_state, timeout))
        rval = 0
        for dev in self._bus.devices:
            rval |= dev.actual
        self.state = rval
        return rval & 0x0F

    @property
    def expected_wkc(self):
        slaves = self.slaves or []
        return 2 * sum(1 for s in slaves if s._dev.spec.obytes) + sum(1 for s in slaves if s._dev.spec.ibytes)

    def send_processdata(self, *, release_gil=None):
        self._check()
        for slave in self.slaves:
            dev = slave._dev
            if dev.index in self._bus.mute or self._bus.lose_frames:
                continue
            if dev.actual == OP_STATE and slave.out_offset is not None:
                new = slave.output[: max(dev.spec.obytes, 1)]
                if self._bus.drop_after_write == dev.index and new != dev.applied_outputs:
                    dev.actual = SAFEOP_STATE | STATE_ERROR
                    dev.applied_outputs = bytes(dev.spec.obytes)
                    continue
                dev.applied_outputs = new
        return 1

    def receive_processdata(self, timeout=2000, *, release_gil=None):
        self._check()
        if self._bus.fail_receive is not None:
            raise self._bus.fail_receive
        if self._bus.lose_frames:
            return EC_NOFRAME  # inputs stay as they were
        wkc = 0
        loop_out = next((s._dev for s in self.slaves if s.name == "EL2008"), None)
        for slave in self.slaves:
            dev = slave._dev
            if not dev.exchanging or dev.index in self._bus.mute:
                continue
            if dev.spec.ibytes:
                if dev.spec.fixed_input is not None:
                    data = dev.spec.fixed_input
                elif dev.spec.name == "EL1008" and loop_out is not None:
                    data = loop_out.applied_outputs
                else:
                    data = (self._bus.cycles & 0xFFFFFFFF).to_bytes(4, "little")[: dev.spec.ibytes]
                self.io_map[slave.in_offset : slave.in_offset + len(data)] = data
                wkc += 1
            if dev.spec.obytes:
                wkc += 2
        self._bus.cycles += 1
        return wkc

    def close(self):
        self._bus.calls.append(("close",))
        self.context_initialized = False


class FakePysoem:
    """Pass an instance where PysoemBackend expects the ``pysoem`` module."""

    SdoError = SdoError
    WkcError = WkcError
    MailboxError = MailboxError
    PacketError = PacketError
    ConfigMapError = ConfigMapError
    NetworkInterfaceNotOpenError = NetworkInterfaceNotOpenError
    NONE_STATE = NONE_STATE
    INIT_STATE = INIT_STATE
    PREOP_STATE = PREOP_STATE
    SAFEOP_STATE = SAFEOP_STATE
    OP_STATE = OP_STATE

    def __init__(
        self,
        specs: list[SlaveSpec] | None = None,
        missing_interfaces: tuple[str, ...] = (),
        power_on_state: int = INIT_STATE,
    ) -> None:
        self.bus = FakeBus(default_bus() if specs is None else specs, power_on_state)
        self.missing_interfaces = set(missing_interfaces)
        self.masters: list[FakeMaster] = []

    def Master(self) -> FakeMaster:  # noqa: N802 - mirrors pysoem.Master
        master = FakeMaster(self)
        self.masters.append(master)
        return master

    @staticmethod
    def al_status_code_to_string(code):
        return al_status_code_to_string(code)

    def device(self, position: int) -> FakeDevice:
        return self.bus.devices[position]
