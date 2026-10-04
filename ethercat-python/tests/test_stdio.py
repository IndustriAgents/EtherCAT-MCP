"""stdout carries JSON-RPC and nothing else."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path


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


def test_server_stdout_is_only_json_rpc(tmp_path: Path) -> None:
    messages = [
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "pytest", "version": "0"},
            },
        },
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "ping", "arguments": {}}},
        # Nothing listens on port 9 (discard): the tool must fail inside the envelope.
        {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "scan_network", "arguments": {}}},
    ]
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
    replies: dict[int, dict] = {}
    try:
        for message in messages:
            proc.stdin.write(json.dumps(message) + "\n")
            proc.stdin.flush()
            if "id" in message:
                line = proc.stdout.readline()
                reply = json.loads(line)  # any non-JSON line on stdout fails here
                assert reply["jsonrpc"] == "2.0"
                replies[reply["id"]] = reply
        rest, stderr = proc.communicate(timeout=30)  # closes stdin: the server exits
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
    assert rest.strip() == ""
    assert replies[1]["result"]["serverInfo"]["version"] == "0.1.0"
    scan = json.loads(replies[4]["result"]["content"][0]["text"])
    assert scan["success"] is False and "not reachable" in scan["error"]
    assert scan["meta"]["backend"] == "mock" and scan["meta"]["simulated"] is True
    assert "NOT from a real EtherCAT bus" in stderr
