"""stdout carries JSON-RPC and nothing else."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
from importlib.metadata import version
from pathlib import Path

import pytest
from conftest import running_mock, server_command


def test_guard_sends_stray_output_to_stderr(tmp_path: Path) -> None:
    code = (
        "import os\n"
        "from ethercat_mcp.server import guard_stdout\n"
        "proto = guard_stdout()\n"
        "print('stray print')\n"
        "os.write(1, b'c-level write\\n')\n"
        'proto.write(b\'{"jsonrpc": "2.0"}\\n\')\n'
        "proto.flush()\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], cwd=tmp_path, capture_output=True, text=True, timeout=60, check=True
    )
    assert result.stdout == '{"jsonrpc": "2.0"}\n'
    assert "stray print" in result.stderr and "c-level write" in result.stderr


ALL_TOOL_CALLS = [
    ("ping", {}),
    ("get_master_status", {}),
    ("scan_network", {}),
    ("get_slave_info", {"slave_position": 3}),
    ("test_slave_communication", {"slave_position": 3}),
    ("read_sdo", {"slave_position": 3, "index": "0x1018", "subindex": 1, "data_type": "UDINT"}),
    ("write_sdo", {"slave_position": 3, "index": "0x8000", "subindex": 6, "value": 1, "data_type": "UINT8"}),
    ("set_slave_state", {"slave_position": 2, "state": "SAFEOP"}),
    ("set_slave_state", {"slave_position": 2, "state": "OP"}),
    ("write_pdo", {"slave_position": 2, "offset": 0, "data": [1]}),
    ("read_pdo", {"slave_position": 2}),
    ("list_slaves", {}),
    ("read_slave_by_alias", {"alias": "outputs"}),
    ("write_slave_by_alias", {"alias": "outputs", "data": [2]}),
    ("load_esi_file", {"filepath": "missing.xml"}),
    ("get_slave_info", {"slave_position": "not a number"}),
    ("no_such_tool", {}),
]


def test_server_stdout_is_only_json_rpc(tmp_path: Path) -> None:
    """Suite rule 5: every tool, unbuffered output, and nothing but JSON-RPC on stdout."""
    slave_map = tmp_path / "slaves.json"
    slave_map.write_text(json.dumps({"outputs": {"position": 2, "offset": 0}}))
    init = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "pytest", "version": "0"},
        },
    }
    messages = [init, {"jsonrpc": "2.0", "method": "notifications/initialized"}]
    messages.append({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    for n, (name, arguments) in enumerate(ALL_TOOL_CALLS, start=3):
        messages.append(
            {"jsonrpc": "2.0", "id": n, "method": "tools/call", "params": {"name": name, "arguments": arguments}}
        )
    with running_mock() as port:
        command, args = server_command()
        env = {
            **os.environ,
            "PYTHONUNBUFFERED": "1",
            "ETHERCAT_BACKEND": "mock",
            "ETHERCAT_MOCK_PORT": str(port),
            "ETHERCAT_WRITES_ENABLED": "true",
            "ETHERCAT_STATE_CHANGE_ENABLED": "true",
            "SLAVE_MAP_FILE": str(slave_map),
        }
        proc = subprocess.Popen(
            [command, *args],
            cwd=tmp_path,
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        replies: dict[int, dict] = {}
        try:
            for message in messages:
                proc.stdin.write(json.dumps(message) + "\n")
                proc.stdin.flush()
                if "id" in message:
                    line = proc.stdout.readline()
                    reply = json.loads(line)  # any non-JSON line on stdout fails here
                    assert reply["jsonrpc"] == "2.0" and reply["id"] == message["id"], line
                    replies[reply["id"]] = reply
            rest, stderr = proc.communicate(timeout=30)  # closes stdin: the server exits
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()
    assert rest.strip() == ""
    assert replies[1]["result"]["serverInfo"]["version"] == version("industriagents-ethercat-mcp")
    assert len(replies[2]["result"]["tools"]) == 11
    envelopes = {
        name: json.loads(replies[n]["result"]["content"][0]["text"])
        for n, (name, _) in enumerate(ALL_TOOL_CALLS, start=3)
        if name != "no_such_tool"
    }
    assert envelopes["write_pdo"]["success"] is True, envelopes["write_pdo"]
    assert all(env["meta"]["backend"] == "mock" for env in envelopes.values())
    assert "NOT from a real EtherCAT bus" in stderr


def test_server_starts_without_a_device(tmp_path: Path) -> None:
    """Suite rule 4: initialize and tools/list work with nothing reachable; tools fail cleanly."""
    messages = [
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "t", "version": "0"},
            },
        },
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        # Nothing listens on port 9 (discard).
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "scan_network", "arguments": {}}},
    ]
    env = {**os.environ, "PYTHONUNBUFFERED": "1", "ETHERCAT_BACKEND": "mock", "ETHERCAT_MOCK_PORT": "9"}
    command, args = server_command()
    proc = subprocess.Popen(
        [command, *args], cwd=tmp_path, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True
    )
    replies: dict[int, dict] = {}
    try:
        for message in messages:
            proc.stdin.write(json.dumps(message) + "\n")
            proc.stdin.flush()
            if "id" in message:
                reply = json.loads(proc.stdout.readline())
                replies[reply["id"]] = reply
        proc.communicate(timeout=30)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
    assert len(replies[2]["result"]["tools"]) == 11
    scan = json.loads(replies[3]["result"]["content"][0]["text"])
    assert scan["success"] is False and "not reachable" in scan["error"]
    assert scan["meta"]["backend"] == "mock" and scan["meta"]["simulated"] is True


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX signals")
@pytest.mark.parametrize("signum", [signal.SIGTERM, signal.SIGINT])
def test_signal_closes_the_master_and_exits(tmp_path: Path, signum: int) -> None:
    """MCP clients stop servers with SIGTERM; the master must still be closed (INIT requested)."""
    env = {**os.environ, "ETHERCAT_BACKEND": "mock", "ETHERCAT_MOCK_PORT": "9"}
    proc = subprocess.Popen(
        [sys.executable, "-m", "ethercat_mcp"],
        cwd=tmp_path,
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        init = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "t", "version": "0"},
            },
        }
        proc.stdin.write(json.dumps(init) + "\n")
        proc.stdin.flush()
        assert "result" in json.loads(proc.stdout.readline())
        proc.send_signal(signum)  # stdin stays open, as when a client kills the server
        assert proc.wait(timeout=20) == 0
        assert "EtherCAT master closed" in proc.stderr.read()
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
