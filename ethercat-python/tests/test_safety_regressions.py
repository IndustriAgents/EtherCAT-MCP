"""Regression tests for the review's blocker and majors, against the fake pysoem.

Each test here was mutation-checked: it fails when the protection it covers is
removed from ec_master.py (see the commit message for the mutations).

1. Stale outputs: SOEM never clears the process image, so a slave re-entering
   OP, or a slave after a rescan, must not get bytes left over from before.
2. First scan of a live segment: a scan resets every slave, so a segment with a
   slave in SAFEOP/OP is only scanned with both gates on.
3. Working counters: process-data results are only reported when the frame's
   working counter confirms them.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fake_pysoem import EC_NOFRAME, FakePysoem

from ethercat_mcp.config import EthercatConfig
from ethercat_mcp.ec_master import (
    INIT_STATE,
    OP_STATE,
    PREOP_STATE,
    SAFEOP_STATE,
    EthercatMasterError,
    PysoemBackend,
)

EL1008, EL2008, EL3102 = 1, 2, 3
GATES_ON = {"ETHERCAT_WRITES_ENABLED": "true", "ETHERCAT_STATE_CHANGE_ENABLED": "true"}


def make(fake: FakePysoem | None = None, **env: str) -> tuple[PysoemBackend, FakePysoem]:
    fake = fake or FakePysoem()
    settings = {"ETHERCAT_INTERFACE": "fake0", "ETHERCAT_TIMEOUT": "100000", **env}
    return PysoemBackend(EthercatConfig.from_env(settings), pysoem_module=fake), fake


@pytest.fixture
def bus() -> Iterator[tuple[PysoemBackend, FakePysoem]]:
    be, fake = make()
    yield be, fake
    be.close()


def up(be: PysoemBackend, position: int, target: int = OP_STATE) -> None:
    be.set_state(position, SAFEOP_STATE)
    if target == OP_STATE:
        be.set_state(position, OP_STATE)


# -- 1. stale outputs -----------------------------------------------------------


def test_reentering_op_does_not_reapply_old_outputs(bus: tuple[PysoemBackend, FakePysoem]) -> None:
    be, fake = bus
    be.scan()
    up(be, EL1008, SAFEOP_STATE)
    up(be, EL2008)
    be.write_pdo(EL2008, 0, b"\xff")
    be._wait_cycles(2)
    assert fake.device(EL2008).applied_outputs == b"\xff"
    be.set_state(EL2008, SAFEOP_STATE)
    assert fake.device(EL2008).applied_outputs == b"\x00"  # the slave's own safe state
    be.set_state(EL2008, OP_STATE)  # no write_pdo in between
    be._wait_cycles(2)
    assert fake.device(EL2008).applied_outputs == b"\x00"
    assert be.read_pdo(EL1008, 0, 1)[0] == b"\x00"


def test_output_image_is_cleared_when_a_slave_leaves_op(bus: tuple[PysoemBackend, FakePysoem]) -> None:
    be, _ = bus
    be.scan()
    up(be, EL2008)
    be.write_pdo(EL2008, 0, b"\x5a")
    be.set_state(EL2008, PREOP_STATE)
    assert be._slaves[EL2008].output == b"\x00"


def test_recovering_to_op_after_a_fault_starts_from_zero(bus: tuple[PysoemBackend, FakePysoem]) -> None:
    """The slave dropped out of OP on its own, so no set_state cleared its outputs on the way down."""
    be, fake = bus
    be.scan()
    up(be, EL1008, SAFEOP_STATE)
    up(be, EL2008)
    fake.bus.drop_after_write = EL2008
    with pytest.raises(EthercatMasterError, match="not OP"):
        be.write_pdo(EL2008, 0, b"\x03")
    assert be._slaves[EL2008].output == b"\x03"  # still in the image
    fake.bus.drop_after_write = None
    be.set_state(EL2008, OP_STATE)  # acknowledges the error and re-enters OP
    be._wait_cycles(2)
    assert fake.device(EL2008).applied_outputs == b"\x00"


def test_process_image_is_zeroed_after_mapping(bus: tuple[PysoemBackend, FakePysoem]) -> None:
    """SOEM never clears the Master's io_map: after a rescan, old bytes would sit at whichever slave now maps there."""
    be, fake = bus
    be.scan()
    up(be, EL2008)
    be.write_pdo(EL2008, 0, b"\xff")
    be.set_state(EL2008, INIT_STATE)
    master = fake.masters[0]
    out = be._slaves[EL2008].out_offset
    master.io_map[out] = 0xA5  # e.g. a different layout after the rescan puts old bytes here
    be.scan()
    be.set_state(EL3102, SAFEOP_STATE)  # maps the image; EL2008 itself is not touched
    assert be._slaves[EL2008].output == b"\x00"
    assert master.io_map[out] == 0


# -- 2. first scan of a live segment -------------------------------------------------


def test_first_scan_refuses_a_live_segment() -> None:
    be, fake = make(FakePysoem(power_on_state=OP_STATE))
    try:
        with pytest.raises(EthercatMasterError, match="Refusing to scan: at least one slave .* SAFEOP or OP"):
            be.scan()
        assert not any(call[0] == "config_init" for call in fake.bus.calls)
        assert fake.device(EL2008).actual == OP_STATE  # untouched
    finally:
        be.close()


@pytest.mark.parametrize(
    "env", [{"ETHERCAT_STATE_CHANGE_ENABLED": "true"}, {"ETHERCAT_WRITES_ENABLED": "true"}], ids=["state", "writes"]
)
def test_one_gate_is_not_enough_to_reset_a_live_segment(env: dict[str, str]) -> None:
    be, fake = make(FakePysoem(power_on_state=SAFEOP_STATE), **env)
    try:
        with pytest.raises(EthercatMasterError, match="Refusing to scan"):
            be.scan()
    finally:
        be.close()


def test_both_gates_allow_resetting_a_live_segment() -> None:
    be, fake = make(FakePysoem(power_on_state=OP_STATE), **GATES_ON)
    try:
        assert {d["state"] for d in be.scan()} == {"PREOP"}
        assert ("master.state_check", INIT_STATE, 2000) in fake.bus.calls
    finally:
        be.close()


def test_first_scan_of_an_idle_segment_is_allowed() -> None:
    be, _ = make(FakePysoem(power_on_state=PREOP_STATE))
    try:
        assert len(be.scan()) == 4
    finally:
        be.close()


# -- 3. working counters ----------------------------------------------------------


def test_read_pdo_fails_when_frames_are_lost(bus: tuple[PysoemBackend, FakePysoem]) -> None:
    be, fake = bus
    be.scan()
    up(be, EL3102, SAFEOP_STATE)
    be.read_pdo(EL3102, 0, None)
    fake.bus.lose_frames = True  # receive_processdata returns EC_NOFRAME, inputs stay frozen
    with pytest.raises(EthercatMasterError, match=f"working counter {EC_NOFRAME}\\); the inputs are stale"):
        be.read_pdo(EL3102, 0, None)


def test_read_pdo_fails_when_a_slave_misses_the_frame(bus: tuple[PysoemBackend, FakePysoem]) -> None:
    be, fake = bus
    be.scan()
    for position in (EL1008, EL2008, EL3102):
        up(be, position, SAFEOP_STATE)
    data, meta = be.read_pdo(EL3102, 0, None)
    assert meta["expected_wkc"] == 4 and meta["wkc"] == 4
    fake.bus.mute.add(EL1008)
    with pytest.raises(EthercatMasterError, match="working counter 3 is below the expected 4"):
        be.read_pdo(EL3102, 0, None)


def test_write_pdo_needs_the_full_working_counter_when_all_slaves_are_in_op(
    bus: tuple[PysoemBackend, FakePysoem],
) -> None:
    be, fake = bus
    be.scan()
    up(be, EL1008)
    up(be, EL3102)
    up(be, EL2008)
    meta = be.write_pdo(EL2008, 0, b"\x01")
    assert meta["verified"] == "expected_wkc" and meta["wkc"] == meta["expected_wkc"] == 4
    fake.bus.mute.add(EL3102)
    with pytest.raises(EthercatMasterError, match="working counter 3 is below the expected 4") as info:
        be.write_pdo(EL2008, 0, b"\x02")
    assert info.value.outcome == "unknown" and info.value.request_sent
    assert "may have been applied; check/read back before retrying" in str(info.value)


def test_write_pdo_checks_the_target_state_when_not_all_slaves_are_in_op(
    bus: tuple[PysoemBackend, FakePysoem],
) -> None:
    be, fake = bus
    be.scan()
    up(be, EL1008, SAFEOP_STATE)
    up(be, EL2008)  # EL1008 is in SAFEOP, EL3102 in PREOP: the group working counter cannot be expected
    meta = be.write_pdo(EL2008, 0, b"\x01")
    assert meta["verified"] == "slave_state" and meta["outcome"] == "written"
    fake.bus.drop_after_write = EL2008  # the slave falls to SAFEOP+ERROR when the new outputs arrive
    with pytest.raises(EthercatMasterError, match="slave 2 is now in SAFEOP\\+ERROR, not OP") as info:
        be.write_pdo(EL2008, 0, b"\x03")
    assert info.value.outcome == "unknown"


def test_write_pdo_with_lost_frames_is_unknown_not_failed(bus: tuple[PysoemBackend, FakePysoem]) -> None:
    be, fake = bus
    be.scan()
    up(be, EL2008)
    fake.bus.lose_frames = True
    with pytest.raises(EthercatMasterError, match="no slave answered the frame") as info:
        be.write_pdo(EL2008, 0, b"\x01")
    assert info.value.outcome == "unknown"
    assert be._slaves[EL2008].output == b"\x01"  # still staged; never rolled back or resent by hand


# -- writes are never repeated; outcomes are reported --------------------------------


def lossy_bus() -> FakePysoem:
    fake = FakePysoem()
    fake.bus.devices[EL3102].spec.sdo_reply_lost = True
    return fake


def test_sdo_download_with_lost_answer_is_unknown_and_not_repeated() -> None:
    be, fake = make(lossy_bus())
    try:
        be.scan()
        with pytest.raises(EthercatMasterError, match="may have been applied; check/read back before retrying") as info:
            be.write_sdo(EL3102, 0x8000, 6, b"\x01")
        assert info.value.outcome == "unknown" and info.value.request_sent
        writes = [call for call in be._slaves[EL3102].sdo_calls if call[0] == "write"]
        assert len(writes) == 1
        assert fake.device(EL3102).objects[(0x8000, 6)][0] == b"\x01"  # it was in fact applied
    finally:
        be.close()


def test_sdo_abort_is_rejected(bus: tuple[PysoemBackend, FakePysoem]) -> None:
    be, _ = bus
    be.scan()
    with pytest.raises(EthercatMasterError, match="aborted: 0x06010002") as info:
        be.write_sdo(EL3102, 0x1000, 0, b"\x00\x00\x00\x00")
    assert info.value.outcome == "rejected"


def test_invalid_state_request_is_rejected(bus: tuple[PysoemBackend, FakePysoem]) -> None:
    be, _ = bus
    be.scan()
    with pytest.raises(EthercatMasterError, match="did not reach OP") as info:
        be.set_state(EL1008, OP_STATE)
    assert info.value.outcome == "rejected"
    assert be.set_state(EL1008, SAFEOP_STATE)["outcome"] == "written"


# -- process-data watchdog ------------------------------------------------------------


def test_watchdog_is_set_on_every_slave_with_outputs_after_mapping() -> None:
    be, fake = make(ETHERCAT_PD_WATCHDOG_MS="80")
    try:
        be.scan()
        be.set_state(EL3102, SAFEOP_STATE)
        calls = [call for call in fake.bus.calls if call[0] == "slave.set_watchdog"]
        assert calls == [("slave.set_watchdog", EL2008, "processdata", 80)]
        assert fake.device(EL2008).watchdog == {"processdata": 80}
        assert be.status()["pd_watchdog_errors"] == {}
    finally:
        be.close()


def test_watchdog_failure_is_reported_not_fatal() -> None:
    fake = FakePysoem()
    fake.bus.devices[EL2008].spec.watchdog_fails = True
    be, _ = make(fake)
    try:
        be.scan()
        be.set_state(EL2008, SAFEOP_STATE)
        assert "2" in be.status()["pd_watchdog_errors"]
    finally:
        be.close()
