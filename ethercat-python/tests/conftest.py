from __future__ import annotations

import json
import queue
import re
import subprocess
import sys
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest

TESTS = Path(__file__).resolve().parent
FIXTURES = TESTS / "fixtures"
REPO = TESTS.parents[1]
MOCK_SCRIPT = REPO / "ethercat-mock-slave" / "ethercat_mock_slave.py"

sys.path.insert(0, str(TESTS))


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture(autouse=True)
def _clean_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tests must not pick up ETHERCAT_* settings from the developer's shell."""
    import os

    for key in list(os.environ):
        if key.startswith(("ETHERCAT_", "SLAVE_MAP_FILE", "MOCK_ETHERCAT_")):
            monkeypatch.delenv(key, raising=False)


def envelope_from(result: Any) -> dict[str, Any]:
    """Pull the envelope out of what FastMCP.call_tool returns (content, or (content, structured))."""
    if isinstance(result, tuple):
        content, structured = result
        if isinstance(structured, dict) and "success" in structured:
            return structured
        result = content
    if isinstance(result, dict):
        return result
    return json.loads(result[0].text)


@contextmanager
def running_mock(*args: str) -> Iterator[int]:
    """Start ethercat-mock-slave (stdlib only, so this venv's Python runs it) on a free loopback port."""
    assert MOCK_SCRIPT.is_file(), MOCK_SCRIPT
    # Suite rule 11: the mock watches the stdin pipe and exits when it closes, so a killed
    # pytest run does not leave it behind.
    proc = subprocess.Popen(
        [
            sys.executable,
            str(MOCK_SCRIPT),
            "--host",
            "127.0.0.1",
            "--port",
            "0",
            "--update-interval",
            "0.2",
            "--exit-on-stdin-eof",
            *args,
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )
    lines: queue.Queue[str] = queue.Queue()
    threading.Thread(target=lambda: [lines.put(line) for line in proc.stderr], daemon=True).start()
    try:
        port = None
        while port is None:
            match = re.search(r"listening on 127\.0\.0\.1:(\d+)", lines.get(timeout=30))
            if match:
                port = int(match.group(1))
        yield port
    finally:
        proc.stdin.close()
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()


def server_command() -> tuple[str, list[str]]:
    """The installed console script if there is one, else ``python -m ethercat_mcp``."""
    script = Path(sys.executable).with_name("ethercat-mcp")
    if script.is_file():
        return str(script), []
    return sys.executable, ["-m", "ethercat_mcp"]


@pytest.fixture(scope="session")
def _canonical_output_validators():
    """Read the reviewed schema once; exercise it with real MCP responses below."""
    import json
    from pathlib import Path

    from jsonschema import Draft202012Validator

    contract = json.loads((Path(__file__).resolve().parents[2] / "contract/tools.json").read_text())
    return {tool["name"]: Draft202012Validator(tool["outputSchema"]) for tool in contract["tools"]}


@pytest.fixture(autouse=True)
def _validate_canonical_wire_results(monkeypatch, _canonical_output_validators):
    """Validate success, error and partial results from real stdio integration calls.

    Hidden legacy aliases can have their own compatibility result format, so this
    enforces the output guarantee only for the advertised canonical tool names.
    """
    import json

    from mcp import ClientSession

    original = ClientSession.call_tool

    async def checked(session, name, *args, **kwargs):
        result = await original(session, name, *args, **kwargs)
        validator = _canonical_output_validators.get(name)
        if validator is not None:
            value = result.structuredContent
            assert isinstance(value, dict), f"{name}: missing structured result"
            validator.validate(value)
            assert result.isError == (value["success"] is False), name
            assert json.loads(result.content[0].text) == value, name
        return result

    monkeypatch.setattr(ClientSession, "call_tool", checked)
