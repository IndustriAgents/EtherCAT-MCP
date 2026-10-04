"""In-memory stand-in for the parts of PySOEM 1.1.12 that PysoemBackend uses.

Real EtherCAT needs raw Ethernet, root and hardware, so the PySOEM code paths
are tested against this fake. It mirrors PySOEM's names and signatures
(``Master.open/config_init/config_map/read_state/write_state/state_check/
send_processdata/receive_processdata/close``, ``CdefSlave.sdo_read/sdo_write/
write_state/state_check``, the ``input``/``output``/``state``/``al_status``
properties and the exception classes) and simulates enough bus behaviour to
check what the backend asks for: the AL state machine, SOEM's automatic state
changes (and ``manual_state_change``), the process image and working counters.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field

NONE_STATE = 0x00
INIT_STATE = 0x01
PREOP_STATE = 0x02
BOOT_STATE = 0x03
SAFEOP_STATE = 0x04
OP_STATE = 0x08
STATE_ACK = 0x10
STATE_ERROR = 0x10

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


class FakeSlave:
    def __init__(self, bus: FakeBus, pos: int, spec: SlaveSpec) -> None:
        self._bus = bus
        self._pos = pos
        self._spec = spec
        self.name = spec.name
        self.man = spec.man
        self.id = spec.id
        self.rev = spec.rev
        self.state = NONE_STATE  # last read state, and the state requested by write_state()
        self.actual = NONE_STATE
        self.al_status = 0
        self._inputs = bytearray(spec.ibytes)
        self._outputs = bytearray(spec.obytes)
        self.applied_outputs = bytes(spec.obytes)
        self.objects = {k: list(v) for k, v in spec.objects.items()}
        self.sdo_calls: list[tuple] = []

    # process image views (empty until config_map, like PySOEM with NULL pointers)
    @property
    def input(self) -> bytes:
        return bytes(self._inputs) if self._bus.mapped else b""

    @property
    def output(self) -> bytes:
        return bytes(self._outputs) if self._bus.mapped else b""

    @output.setter
    def output(self, value: bytes) -> None:
        assert isinstance(value, bytes)
        self._outputs[: len(value)] = value

    def _request(self, target: int) -> None:
        if target == self.actual:
            self.al_status = 0
            return
        if (self.actual, target) not in VALID or (target in (SAFEOP_STATE, OP_STATE) and not self._bus.mapped):
            self.al_status = 0x0011
            return
        if self.actual == OP_STATE:
            self.applied_outputs = bytes(len(self._outputs))
        self.actual = target
        self.al_status = 0

    def write_state(self) -> int:
        self._bus.calls.append(("slave.write_state", self._pos, self.state))
        self._request(self.state)
        return 1

    def state_check(self, expected_state: int, timeout: int = 2000) -> int:
        self.state = self.actual
        return self.actual

    def _mailbox(self) -> None:
        if not self._spec.mailbox or self.actual in (NONE_STATE, INIT_STATE):
            raise WkcError(wkc=0)

    def sdo_read(self, index, subindex, size=0, ca=False, *, release_gil=None) -> bytes:
        self.sdo_calls.append(("read", index, subindex, release_gil))
        self._mailbox()
        if (index, subindex) not in self.objects:
            raise SdoError(self._pos + 1, index, subindex, 0x06020000, "Object does not exist")
        return bytes(self.objects[(index, subindex)][0])

    def sdo_write(self, index, subindex, data: bytes, ca=False, *, release_gil=None) -> None:
        assert isinstance(data, bytes)
        self.sdo_calls.append(("write", index, subindex, data, release_gil))
        self._mailbox()
        if (index, subindex) not in self.objects:
            raise SdoError(self._pos + 1, index, subindex, 0x06020000, "Object does not exist")
        entry = self.objects[(index, subindex)]
        if not entry[1]:
            raise SdoError(self._pos + 1, index, subindex, 0x06010002, "Attempt to write a read only object")
        entry[0] = data


class FakeBus:
    def __init__(self, specs: list[SlaveSpec]) -> None:
        self.specs = specs
        self.mapped = False
        self.calls: list[tuple] = []
        self.fail_receive: Exception | None = None
        self.cycles = 0


class FakeMaster:
    def __init__(self, pysoem: FakePysoem) -> None:
        self._pysoem = pysoem
        self._bus = pysoem.bus
        self.slaves = None
        self.manual_state_change = 0
        self.state = NONE_STATE
        self.opened = False
        self._lock = threading.Lock()

    def open(self, ifname, ifname_red=None):
        self._bus.calls.append(("open", ifname))
        if ifname in self._pysoem.missing_interfaces:
            raise ConnectionError(f"could not open interface {ifname}")
        self.opened = True

    def _check(self):
        if not self.opened:
            raise RuntimeError("SOEM Network interface is not initialized or has been closed")

    def config_init(self, usetable=False, *, release_gil=None):
        self._check()
        self._bus.calls.append(("config_init", self.manual_state_change))
        self._bus.mapped = False
        self.slaves = [FakeSlave(self._bus, i, spec) for i, spec in enumerate(self._bus.specs)]
        for slave in self.slaves:
            slave.actual = INIT_STATE  # SOEM resets every slave to INIT
            if not self.manual_state_change:
                slave.actual = PREOP_STATE
            slave.state = slave.actual
        return len(self.slaves) if self.slaves else -1

    def config_map(self):
        self._check()
        self._bus.calls.append(("config_map", self.manual_state_change))
        self._bus.mapped = True
        if not self.manual_state_change:
            for slave in self.slaves:
                slave._request(SAFEOP_STATE)
        return sum(s._spec.ibytes + s._spec.obytes for s in self.slaves)

    def read_state(self):
        self._check()
        for slave in self.slaves or []:
            slave.state = slave.actual
        return min((s.actual for s in self.slaves or []), default=NONE_STATE)

    def write_state(self):
        self._check()
        self._bus.calls.append(("master.write_state", self.state))
        for slave in self.slaves or []:
            slave._request(self.state)
        return len(self.slaves or [])

    def state_check(self, expected_state, timeout=50000):
        return self.read_state()

    @property
    def expected_wkc(self):
        slaves = self.slaves or []
        return 2 * sum(1 for s in slaves if s._spec.obytes) + sum(1 for s in slaves if s._spec.ibytes)

    def send_processdata(self, *, release_gil=None):
        self._check()
        with self._lock:
            for slave in self.slaves:
                if slave.actual == OP_STATE:
                    slave.applied_outputs = bytes(slave._outputs)
        return 1

    def receive_processdata(self, timeout=2000, *, release_gil=None):
        self._check()
        if self._bus.fail_receive is not None:
            raise self._bus.fail_receive
        wkc = 0
        with self._lock:
            loop_out = next((s for s in self.slaves if s.name == "EL2008"), None)
            for slave in self.slaves:
                if slave.actual not in (SAFEOP_STATE, OP_STATE):
                    continue
                if slave._spec.ibytes:
                    if slave.name == "EL1008" and loop_out is not None:
                        slave._inputs[:] = loop_out.applied_outputs
                    elif slave.name == "EL3102":
                        slave._inputs[:] = (self._bus.cycles & 0xFFFFFFFF).to_bytes(4, "little")
                    wkc += 1
                if slave._spec.obytes:
                    wkc += 2
            self._bus.cycles += 1
        return wkc

    def close(self):
        self._bus.calls.append(("close",))
        self.opened = False


class FakePysoem:
    """Pass an instance where PysoemBackend expects the ``pysoem`` module."""

    SdoError = SdoError
    WkcError = WkcError
    MailboxError = MailboxError
    PacketError = PacketError
    ConfigMapError = ConfigMapError
    NONE_STATE = NONE_STATE
    INIT_STATE = INIT_STATE
    PREOP_STATE = PREOP_STATE
    SAFEOP_STATE = SAFEOP_STATE
    OP_STATE = OP_STATE

    def __init__(self, specs: list[SlaveSpec] | None = None, missing_interfaces: tuple[str, ...] = ()) -> None:
        self.bus = FakeBus(default_bus() if specs is None else specs)
        self.missing_interfaces = set(missing_interfaces)
        self.masters: list[FakeMaster] = []

    def Master(self) -> FakeMaster:  # noqa: N802 - mirrors pysoem.Master
        master = FakeMaster(self)
        self.masters.append(master)
        return master

    @staticmethod
    def al_status_code_to_string(code):
        return al_status_code_to_string(code)
