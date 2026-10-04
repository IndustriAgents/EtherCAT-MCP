"""PySOEM code paths, exercised against a fake pysoem module (known issue 2).

Nothing here opens a network interface.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fake_pysoem import FakePysoem

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
    assert meta["wkc"] > 0 and meta["expected_wkc"] == 4
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
        with pytest.raises(EthercatMasterError, match="below ETHERCAT_EXPECTED_WKC=99"):
            be.write_pdo(2, 0, b"\x01")
    finally:
        be.close()


def test_invalid_state_change_is_an_error(backend: PysoemBackend) -> None:
    backend.scan()
    with pytest.raises(EthercatMasterError, match=r"did not reach OP; it is in PREOP \(AL status 0x0011"):
        backend.set_state(1, OP_STATE)


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
    assert ("master.write_state", 0x01) in fake.bus.calls
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
