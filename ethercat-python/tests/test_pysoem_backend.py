"""PySOEM code paths, exercised against a fake pysoem module (known issue 2).

Nothing here opens a network interface.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fake_pysoem import FakePysoem, SlaveSpec

from ethercat_mcp.config import EthercatConfig
from ethercat_mcp.ec_master import (
    OP_STATE,
    PREOP_STATE,
    SAFEOP_STATE,
    EthercatMaster,
    EthercatMasterError,
    GateError,
    PysoemBackend,
)


def make(fake: FakePysoem | None = None, **env: str) -> tuple[PysoemBackend, FakePysoem]:
    fake = fake or FakePysoem()
    settings = {"ETHERCAT_INTERFACE": "fake0", "ETHERCAT_TIMEOUT": "100000", **env}
    return PysoemBackend(EthercatConfig.from_env(settings), pysoem_module=fake), fake


@pytest.fixture
def backend() -> Iterator[PysoemBackend]:
    be, _ = make()
    yield be
    be.close()


def bring_up(be: PysoemBackend, position: int, target: int = OP_STATE) -> None:
    be.set_state(position, SAFEOP_STATE)
    if target == OP_STATE:
        be.set_state(position, OP_STATE)


def test_open_failure_is_reported() -> None:
    be, _ = make(FakePysoem(missing_interfaces=("fake0",)))
    with pytest.raises(EthercatMasterError, match="Failed to open interface fake0: could not open interface fake0"):
        be.scan()


def test_scan_reports_identity_and_preop(backend: PysoemBackend) -> None:
    devices = backend.scan()
    assert [d["name"] for d in devices] == ["EK1100", "EL1008", "EL2008", "EL3102"]
    assert devices[1]["vendor_id"] == "0x00000002"
    assert devices[1]["product_code"] == "0x03F03052"
    assert {d["state"] for d in devices} == {"PREOP"}
    # Sizes are unknown until the process image is mapped: no made-up numbers.
    assert devices[1]["input_size"] is None


def test_tools_need_a_scan_first(backend: PysoemBackend) -> None:
    with pytest.raises(EthercatMasterError, match="call scan_network first"):
        backend.read_sdo(0, 0x1000, 0)
    with pytest.raises(EthercatMasterError, match="call scan_network first"):
        backend.read_states()


def test_positions_are_range_checked(backend: PysoemBackend) -> None:
    backend.scan()
    for position in (-1, 4):
        with pytest.raises(EthercatMasterError, match="out of range"):
            backend.read_sdo(position, 0x1000, 0)


def test_read_pdo_needs_a_mapped_image_and_safeop(backend: PysoemBackend) -> None:
    backend.scan()
    with pytest.raises(EthercatMasterError, match="not mapped"):
        backend.read_pdo(1, 0, 1)
    backend.set_state(3, SAFEOP_STATE)
    with pytest.raises(EthercatMasterError, match="Slave 1 is in PREOP. Inputs are only valid in SAFEOP or OP"):
        backend.read_pdo(1, 0, 1)


def test_mapping_does_not_push_every_slave_to_safeop(backend: PysoemBackend) -> None:
    backend.scan()
    fake = backend._pysoem
    backend.set_state(3, SAFEOP_STATE)
    assert ("config_map", 1) in fake.bus.calls  # manual_state_change while mapping
    states = {s["position"]: s["state"] for s in backend.read_states()}
    assert states == {0: "PREOP", 1: "PREOP", 2: "PREOP", 3: "SAFEOP"}
    status = backend.status()
    assert status["process_image_mapped"] and status["cyclic_exchange_running"]


def test_read_pdo_returns_the_process_image(backend: PysoemBackend) -> None:
    backend.scan()
    backend.set_state(3, SAFEOP_STATE)
    first, meta = backend.read_pdo(3, 0, None)
    second, _ = backend.read_pdo(3, 0, 4)
    assert len(first) == 4 and first != second  # the fake counts cycles into the EL3102 inputs
    # Not every process-data slave is up (EL1008/EL2008 are in PREOP), so only wkc > 0 is enforced.
    assert meta["wkc"] > 0 and meta["expected_wkc"] is None
    assert len(backend.read_pdo(3, 2, 2)[0]) == 2
    with pytest.raises(EthercatMasterError, match="has 4 input byte"):
        backend.read_pdo(3, 2, 3)


def test_write_pdo_only_in_op_and_reaches_the_bus(backend: PysoemBackend) -> None:
    backend.scan()
    bring_up(backend, 1, SAFEOP_STATE)
    backend.set_state(2, SAFEOP_STATE)
    with pytest.raises(EthercatMasterError, match="Outputs are only applied in OP"):
        backend.write_pdo(2, 0, b"\xa5")
    result = backend.set_state(2, OP_STATE)
    assert result["state"] == "OP" and result["requested"] == "OP"
    meta = backend.write_pdo(2, 0, b"\xa5")
    assert meta["wkc"] > 0
    data, _ = backend.read_pdo(1, 0, 1)  # EL2008 outputs are looped back to EL1008 in the fake
    assert data == b"\xa5"
    with pytest.raises(EthercatMasterError, match="has 1 output byte"):
        backend.write_pdo(2, 1, b"\x01")
    backend.set_state(1, OP_STATE)
    with pytest.raises(EthercatMasterError, match="no output process data"):
        backend.write_pdo(1, 0, b"\x01")


def test_write_pdo_checks_expected_wkc() -> None:
    be, _ = make(ETHERCAT_EXPECTED_WKC="99")
    try:
        be.scan()
        bring_up(be, 2)
        with pytest.raises(EthercatMasterError, match="below the expected 99") as info:
            be.write_pdo(2, 0, b"\x01")
        assert info.value.outcome == "unknown"
    finally:
        be.close()


def test_invalid_state_change_is_an_error_and_the_error_is_acknowledged(backend: PysoemBackend) -> None:
    backend.scan()
    with pytest.raises(EthercatMasterError, match=r"did not reach OP; it is in PREOP\+ERROR \(AL status 0x0011"):
        backend.set_state(1, OP_STATE)
    # The slave now ignores requests until its error is acknowledged; set_state sends the ACK bit.
    assert backend.set_state(1, SAFEOP_STATE)["state"] == "SAFEOP"
    fake = backend._pysoem
    assert ("slave.write_state", 1, SAFEOP_STATE | 0x10) in fake.bus.calls


def test_state_polling_does_not_hold_the_gil_for_long(backend: PysoemBackend) -> None:
    backend.scan()
    backend.set_state(3, SAFEOP_STATE)
    checks = [call for call in backend._pysoem.bus.calls if call[0] == "slave.state_check"]
    assert checks and all(call[3] <= 1000 for call in checks)
    assert ("config_init", 0, True) in backend._pysoem.bus.calls  # release_gil during the scan


def test_rescan_is_refused_by_the_backend_while_process_data_runs(backend: PysoemBackend) -> None:
    backend.scan()
    backend.set_state(3, SAFEOP_STATE)
    with pytest.raises(EthercatMasterError, match=r"Refusing to rescan: slave\(s\) 3 \(SAFEOP\)"):
        backend.scan()
    backend.set_state(3, PREOP_STATE)
    assert len(backend.scan()) == 4


def bit_bus(fprd_fails: bool = False) -> FakePysoem:
    return FakePysoem(
        [
            SlaveSpec("EK1100", 2, 0x044C2C52, 0x00110000),
            # Two 4-bit terminals sharing one process-image byte each way, as SOEM packs them.
            SlaveSpec(
                "EL1004", 2, 0x03EC3052, 0, ibytes=1, fmmus=[(1, 1, 4, 7)], fixed_input=b"\xa5", fprd_fails=fprd_fails
            ),
            SlaveSpec("EL2004", 2, 0x07D43052, 0, obytes=1, fmmus=[(2, 1, 0, 3)], fprd_fails=fprd_fails),
            SlaveSpec("EL1008", 2, 0x03F03052, 0, ibytes=1, fixed_input=b"\x3c"),
        ]
    )


def test_bit_packed_inputs_are_masked_and_outputs_refused() -> None:
    be, _ = make(bit_bus())
    try:
        be.scan()
        for position in (1, 2, 3):
            be.set_state(position, SAFEOP_STATE)
        data, meta = be.read_pdo(1, 0, None)
        assert data == b"\x0a"  # bits 4-7 of 0xA5, shifted down; the low nibble belongs to a neighbour
        assert meta["bit_packed"] == {"first_bit": 4, "bit_count": 4}
        assert be.read_pdo(3, 0, 1)[0] == b"\x3c"  # byte-aligned slaves are untouched
        be.set_state(2, OP_STATE)
        with pytest.raises(EthercatMasterError, match="bits 0-3 of a process-image byte shared with other slaves"):
            be.write_pdo(2, 0, b"\x01")
    finally:
        be.close()


def test_unknown_alignment_is_refused() -> None:
    be, _ = make(bit_bus(fprd_fails=True))
    try:
        be.scan()
        be.set_state(1, SAFEOP_STATE)
        with pytest.raises(
            EthercatMasterError, match="Cannot tell whether slave 1's input process data is byte-aligned"
        ):
            be.read_pdo(1, 0, 1)
    finally:
        be.close()


def test_sdo_read_write_and_errors(backend: PysoemBackend) -> None:
    backend.scan()
    assert backend.read_sdo(3, 0x1018, 1) == b"\x02\x00\x00\x00"
    backend.write_sdo(3, 0x8000, 6, b"\x01")
    assert backend.read_sdo(3, 0x8000, 6) == b"\x01"
    slave = backend._slaves[3]
    assert all(call[-1] is True for call in slave.sdo_calls)  # release_gil, so process data keeps flowing
    with pytest.raises(EthercatMasterError, match="aborted: 0x06020000"):
        backend.read_sdo(3, 0x2000, 0)
    with pytest.raises(EthercatMasterError, match="aborted: 0x06010002"):
        backend.write_sdo(3, 0x1000, 0, b"\x00\x00\x00\x00")
    with pytest.raises(EthercatMasterError, match="no answer"):
        backend.read_sdo(0, 0x1000, 0)  # EK1100: no mailbox


def test_cycle_failure_is_reported(backend: PysoemBackend) -> None:
    backend.scan()
    backend.set_state(3, SAFEOP_STATE)
    backend._pysoem.bus.fail_receive = OSError("link down")
    with pytest.raises(EthercatMasterError, match="link down"):
        for _ in range(100):
            backend.read_pdo(3, 0, 1)
    assert "link down" in backend.status()["cycle_error"]


def test_close_requests_init_and_stops_the_exchange() -> None:
    be, fake = make()
    be.scan()
    be.set_state(3, SAFEOP_STATE)
    thread = be._cycle_thread
    be.close()
    assert thread is not None and not thread.is_alive()
    assert ("master.write_state", 0x11) in fake.bus.calls  # INIT with error acknowledge
    assert fake.bus.calls[-1] == ("close",)
    assert be.status()["interface_open"] is False


def test_close_without_process_data_does_not_touch_states() -> None:
    be, fake = make()
    be.scan()
    be.close()
    assert not any(call[0] == "master.write_state" for call in fake.bus.calls)


@pytest.mark.anyio
async def test_facade_gates_and_rescan_guard() -> None:
    be, fake = make()
    master = EthercatMaster(be.config, backend=be)
    try:
        with pytest.raises(GateError, match="ETHERCAT_WRITES_ENABLED"):
            await master.write_pdo(2, 0, b"\x01")
        with pytest.raises(GateError, match="ETHERCAT_STATE_CHANGE_ENABLED"):
            await master.set_slave_state(2, OP_STATE)
        assert ("open", "fake0") not in fake.bus.calls  # refused before touching the bus

        await master.scan_slaves()
        enabled = EthercatMaster(
            EthercatConfig.from_env(
                {
                    "ETHERCAT_INTERFACE": "fake0",
                    "ETHERCAT_WRITES_ENABLED": "true",
                    "ETHERCAT_STATE_CHANGE_ENABLED": "true",
                    "ETHERCAT_TIMEOUT": "100000",
                }
            ),
            backend=be,
        )
        await enabled.set_slave_state(2, SAFEOP_STATE)
        with pytest.raises(EthercatMasterError, match="Refusing to rescan: slave\\(s\\) 2 \\(SAFEOP\\)"):
            await enabled.scan_slaves()
        await enabled.set_slave_state(2, PREOP_STATE)
        devices, meta = await enabled.scan_slaves()
        assert meta["count"] == 4 and devices[2]["state"] == "PREOP"

        info, _ = await enabled.test_communication(3)
        assert info["coe"] == {"ok": True, "device_type": "0x01011389"}
        info, _ = await enabled.test_communication(0)
        assert info["responding"] is True and info["coe"]["ok"] is False
    finally:
        await master.close()
