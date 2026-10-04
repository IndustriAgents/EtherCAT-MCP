"""End to end: the real ``ethercat-mcp`` over stdio, driven by the MCP client SDK, against the mock slave.

Starts ``ethercat-mock-slave`` (standard library only, so this venv's Python
can run it) on a free loopback port, then the server with
``ETHERCAT_BACKEND=mock``. Every tool is called at least once.
"""

from __future__ import annotations

import json
import queue
import re
import subprocess
import sys
import threading
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest
from conftest import FIXTURES, MOCK_SCRIPT
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from test_tools import EXPECTED_TOOLS


@pytest.fixture(scope="module")
def mock_port() -> Iterator[int]:
    assert MOCK_SCRIPT.is_file(), MOCK_SCRIPT
    proc = subprocess.Popen(
        [sys.executable, str(MOCK_SCRIPT), "--host", "127.0.0.1", "--port", "0", "--update-interval", "0.2"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )
    lines: queue.Queue[str] = queue.Queue()
    threading.Thread(target=lambda: [lines.put(line) for line in proc.stderr], daemon=True).start()
    try:
        port = None
        while port is None:
            line = lines.get(timeout=30)
            match = re.search(r"listening on 127\.0\.0\.1:(\d+)", line)
            if match:
                port = int(match.group(1))
        yield port
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()


def server_command() -> tuple[str, list[str]]:
    script = Path(sys.executable).with_name("ethercat-mcp")
    if script.is_file():
        return str(script), []
    return sys.executable, ["-m", "ethercat_mcp"]


@asynccontextmanager
async def session(tmp_path: Path, port: int, **env: str) -> AsyncIterator[ClientSession]:
    slave_map = tmp_path / "slaves.json"
    slave_map.write_text(
        json.dumps(
            {
                "inputs": {"position": 1, "offset": 0, "length": 1, "description": "EL1008"},
                "outputs": {"position": 2, "offset": 0, "description": "EL2008"},
                "broken": {"position": "abc"},
            }
        )
    )
    command, args = server_command()
    params = StdioServerParameters(
        command=command,
        args=args,
        cwd=str(tmp_path),  # no stray .env from the developer's checkout
        env={
            "ETHERCAT_BACKEND": "mock",
            "ETHERCAT_MOCK_HOST": "127.0.0.1",
            "ETHERCAT_MOCK_PORT": str(port),
            "ETHERCAT_TIMEOUT": "5000000",
            "SLAVE_MAP_FILE": str(slave_map),
            "ETHERCAT_ESI_PATH": str(FIXTURES),
            **env,
        },
    )
    with (tmp_path / "server-stderr.log").open("w") as errlog:
        async with stdio_client(params, errlog=errlog) as (read, write):
            async with ClientSession(read, write) as client:
                yield client


async def call(client: ClientSession, tool: str, **arguments: Any) -> dict[str, Any]:
    result = await client.call_tool(tool, arguments)
    assert not result.isError, result
    envelope = json.loads(result.content[0].text)
    assert set(envelope) == {"success", "data", "error", "meta"}, envelope
    # Mock answers must never look like a real bus.
    assert envelope["meta"]["backend"] == "mock", envelope
    assert envelope["meta"]["simulated"] is True, envelope
    return envelope


def ok(envelope: dict[str, Any]) -> dict[str, Any]:
    assert envelope["success"] is True, envelope
    return envelope["data"]


def failed(envelope: dict[str, Any], text: str) -> None:
    assert envelope["success"] is False, envelope
    assert text in envelope["error"], envelope


@pytest.mark.anyio
async def test_read_only_server_against_mock(tmp_path: Path, mock_port: int) -> None:
    async with session(tmp_path, mock_port) as client:
        init = await client.initialize()
        assert init.serverInfo.name == "EtherCAT MCP Server"
        assert init.serverInfo.version == "0.1.0"
        tools = (await client.list_tools()).tools
        assert {t.name for t in tools} == EXPECTED_TOOLS
        assert all(t.description for t in tools)

        ping = ok(await call(client, "ping"))
        assert ping["backend"] == "mock" and ping["simulated"] is True
        assert ping["writes_enabled"] is False and ping["state_change_enabled"] is False
        assert ok(await call(client, "get_master_status"))["mock_endpoint"] == f"127.0.0.1:{mock_port}"

        failed(await call(client, "get_slave_info", slave_position=0), "call scan_network first")
        slaves = ok(await call(client, "scan_network"))["slaves"]
        assert [s["name"] for s in slaves] == ["EK1100", "EL1008", "EL2008", "EL3102"]
        assert {s["state"] for s in slaves} == {"PREOP"}
        assert ok(await call(client, "get_slave_info", slave_position=3))["product_code"] == "0x0C1E3052"
        failed(await call(client, "get_slave_info", slave_position=-1), "below the minimum 0")
        failed(await call(client, "get_slave_info", slave_position=4), "out of range")
        failed(await call(client, "get_slave_info", slave_position="x"), "Invalid arguments for get_slave_info")

        vendor = ok(await call(client, "read_sdo", slave_position=3, index="0x1018", subindex=1, data_type="UDINT"))
        assert vendor["value"] == 2
        name = ok(await call(client, "read_sdo", slave_position=3, index="0x1008", data_type="STRING"))
        assert name["value"] == "EL3102"
        failed(await call(client, "read_sdo", slave_position=3, index="not-hex"), "is not a number")
        failed(await call(client, "read_sdo", slave_position=3, index="0x2000"), "SDO abort 0x06020000")
        failed(await call(client, "read_sdo", slave_position=0, index="0x1000"), "has no mailbox")
        assert ok(await call(client, "test_slave_communication", slave_position=3))["coe"]["ok"] is True
        assert ok(await call(client, "test_slave_communication", slave_position=0))["coe"]["ok"] is False

        failed(await call(client, "read_pdo", slave_position=1), "inputs are only valid in SAFEOP or OP")
        failed(await call(client, "write_pdo", slave_position=2, offset=0, data=[1]), "ETHERCAT_WRITES_ENABLED")
        failed(
            await call(client, "write_sdo", slave_position=3, index="0x8000", subindex=6, value=1, data_type="BOOL"),
            "ETHERCAT_WRITES_ENABLED",
        )
        failed(await call(client, "set_slave_state", slave_position=2, state="OP"), "ETHERCAT_STATE_CHANGE_ENABLED")
        failed(await call(client, "write_slave_by_alias", alias="outputs", data=[1]), "ETHERCAT_WRITES_ENABLED")

        listing = ok(await call(client, "list_slaves"))
        assert listing["count"] == 3
        failed(await call(client, "read_slave_by_alias", alias="broken"), "is not a number")
        failed(await call(client, "read_slave_by_alias", alias="inputs"), "only valid in SAFEOP or OP")

        esi = ok(await call(client, "load_esi_file", filepath="sample_esi.xml", device_type="EL1008"))
        assert esi["devices"][0]["pdos"]["tx"][0]["entries"][0]["index"] == "0x6000"


@pytest.mark.anyio
async def test_writes_and_state_changes_against_mock(tmp_path: Path, mock_port: int) -> None:
    async with session(
        tmp_path, mock_port, ETHERCAT_WRITES_ENABLED="true", ETHERCAT_STATE_CHANGE_ENABLED="true"
    ) as client:
        await client.initialize()
        ok(await call(client, "scan_network"))

        failed(await call(client, "set_slave_state", slave_position=2, state="OP"), "invalid requested state change")
        for state in ("SAFEOP", "OP"):
            assert ok(await call(client, "set_slave_state", slave_position=2, state=state))["state"] == state
        assert ok(await call(client, "set_slave_state", slave_position=1, state="SAFEOP"))["state"] == "SAFEOP"
        failed(await call(client, "scan_network"), "Refusing to rescan")

        wrote = ok(await call(client, "write_pdo", slave_position=2, offset=0, data=[0xA5]))
        assert wrote["written_bytes"] == 1
        assert ok(await call(client, "read_pdo", slave_position=1, offset=0, length=1))["raw_data_hex"] == "a5"
        failed(await call(client, "write_pdo", slave_position=2, offset=1, data=[1]), "out of range")
        failed(await call(client, "write_pdo", slave_position=2, offset=0, data=[300]), "not a byte value")

        ok(await call(client, "write_slave_by_alias", alias="outputs", data=[0x3C]))
        assert ok(await call(client, "read_slave_by_alias", alias="inputs"))["raw_data_hex"] == "3c"

        ok(
            await call(
                client, "write_sdo", slave_position=3, index="0x8000", subindex=0x15, value=500, data_type="UINT"
            )
        )
        assert ok(await call(client, "read_sdo", slave_position=3, index="0x8000", subindex=0x15))["value"] == 500
        failed(
            await call(client, "write_sdo", slave_position=3, index="0x1000", subindex=0, value=1, data_type="UDINT"),
            "SDO abort 0x06010002",
        )
        failed(
            await call(client, "write_sdo", slave_position=3, index="0x8000", subindex=6, value=1, data_type="UINT"),
            "SDO abort 0x06070010",
        )

        # Leaving OP drops the outputs to their safe state; the looped-back input follows.
        assert ok(await call(client, "set_slave_state", slave_position=2, state="PREOP"))["state"] == "PREOP"
        assert ok(await call(client, "read_pdo", slave_position=1))["raw_data_hex"] == "00"
        failed(await call(client, "write_pdo", slave_position=2, offset=0, data=[1]), "only applied in OP")

        ok(await call(client, "set_slave_state", slave_position=1, state="INIT"))
        ok(await call(client, "set_slave_state", slave_position=2, state="INIT"))
        rescan = ok(await call(client, "scan_network"))
        assert {s["state"] for s in rescan["slaves"]} == {"PREOP"}
