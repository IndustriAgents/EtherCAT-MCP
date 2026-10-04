"""The simulated segment and its JSON API."""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ethercat_mock_slave import EthercatMockServer, MockConfig, parse_args  # noqa: E402


def mock() -> EthercatMockServer:
    return EthercatMockServer(MockConfig(port=0))


def ok(server: EthercatMockServer, **request: object) -> object:
    reply = server.dispatch(request)
    assert reply["success"] is True, reply
    return reply["data"]


def err(server: EthercatMockServer, text: str, **request: object) -> dict:
    reply = server.dispatch(request)
    assert reply["success"] is False, reply
    assert text in reply["error"], reply
    return reply


def test_power_on_state_is_init_and_scan_brings_preop() -> None:
    server = mock()
    assert {s["state"] for s in ok(server, op="read_state")} == {"INIT"}
    slaves = ok(server, op="scan")
    assert [s["name"] for s in slaves] == ["EK1100", "EL1008", "EL2008", "EL3102"]
    assert slaves[3]["vendor_id"] == "0x00000002" and slaves[3]["input_size"] == 8
    assert {s["state"] for s in slaves} == {"PREOP"}


def test_state_machine() -> None:
    server = mock()
    ok(server, op="scan")
    reply = err(server, "invalid requested state change PREOP -> OP", op="set_state", position=2, state="OP")
    assert reply["al_status_code"] == 0x0011 and reply["state"] == "PREOP"
    assert ok(server, op="set_state", position=2, state_code=0x04)["state"] == "SAFEOP"
    assert ok(server, op="set_state", position=2, state="OP")["al_status_code"] == 0
    err(server, "not supported", op="set_state", position=2, state_code=0x03)
    err(server, "out of range", op="set_state", position=7, state="OP")


def test_process_data_rules_and_loopback() -> None:
    server = mock()
    ok(server, op="scan")
    err(server, "inputs are only valid in SAFEOP or OP", op="read_pdo", position=1)
    for state in ("SAFEOP", "OP"):
        ok(server, op="set_state", position=2, state=state)
    ok(server, op="set_state", position=1, state="SAFEOP")
    assert ok(server, op="write_pdo", position=2, offset=0, data=[0x81])["written_bytes"] == 1
    assert ok(server, op="read_pdo", position=1)["raw_data_hex"] == "81"
    err(server, "out of range", op="write_pdo", position=2, offset=1, data=[1])
    err(server, "byte values", op="write_pdo", position=2, data=[256])
    ok(server, op="set_state", position=2, state="SAFEOP")
    assert ok(server, op="read_pdo", position=1)["raw_data_hex"] == "00"  # outputs dropped when leaving OP
    err(server, "only applied in OP", op="write_pdo", position=2, data=[1])
    ok(server, op="set_state", position=3, state="SAFEOP")
    assert len(ok(server, op="read_pdo", position=3, offset=2, length=2)["raw_data_hex"]) == 4


def test_sdo() -> None:
    server = mock()
    err(server, "is in INIT", op="read_sdo", position=3, index=0x1000)
    ok(server, op="scan")
    assert ok(server, op="read_sdo", position=3, index=0x1018, subindex=2)["data_hex"] == "52301e0c"
    err(server, "has no mailbox", op="read_sdo", position=1, index=0x1000)
    assert err(server, "0x06020000", op="read_sdo", position=3, index=0x2000)["abort_code"] == "0x06020000"
    err(server, "0x06090011", op="read_sdo", position=3, index=0x1018, subindex=9)
    err(server, "0x06010002", op="write_sdo", position=3, index=0x1000, subindex=0, data_hex="00000000")
    err(server, "0x06070010", op="write_sdo", position=3, index=0x8000, subindex=6, data_hex="0000")
    ok(server, op="write_sdo", position=3, index=0x8000, subindex=6, data_hex="01")
    assert ok(server, op="read_sdo", position=3, index=0x8000, subindex=6)["data_hex"] == "01"


def test_bad_requests_do_not_raise() -> None:
    server = mock()
    err(server, "Unknown op", op="explode")
    err(server, "Missing field 'position'", op="read_pdo")
    err(server, "must be an integer", op="read_pdo", position="1")
    assert server.dispatch([1, 2])["success"] is False


def test_tcp_round_trip() -> None:
    async def scenario() -> None:
        server = mock()
        await server.start()
        reader, writer = await asyncio.open_connection("127.0.0.1", server.config.port)

        async def ask(raw: bytes) -> dict:
            writer.write(raw + b"\n")
            await writer.drain()
            return json.loads(await asyncio.wait_for(reader.readline(), timeout=10))

        try:
            assert (await ask(b'{"op": "ping"}'))["data"]["mock"] is True
            assert (await ask(b"not json")) == {"success": False, "error": "Invalid JSON"}
            assert len((await ask(b'{"op": "scan"}'))["data"]) == 4
        finally:
            writer.close()
            await writer.wait_closed()
            await server.stop()

    asyncio.run(scenario())


def test_config_is_read_when_parsing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MOCK_ETHERCAT_PORT", "16701")
    monkeypatch.setenv("MOCK_ETHERCAT_VERBOSE", "true")
    args = parse_args([])
    assert args.port == 16701 and args.verbose is True
    assert parse_args(["--port", "0"]).port == 0


def test_help() -> None:
    script = Path(__file__).resolve().parents[1] / "ethercat_mock_slave.py"
    result = subprocess.run([sys.executable, str(script), "--help"], capture_output=True, text=True, timeout=60)
    assert result.returncode == 0 and "--update-interval" in result.stdout
