"""CLI arguments and .env handling (known issues 4 and 10; review finding 3; suite rule 3)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from importlib.metadata import version
from pathlib import Path

CLI = [sys.executable, "-m", "ethercat_mcp"]


def run(*args: str, cwd: Path, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [*CLI, *args],
        cwd=cwd,
        env={**os.environ, **(env or {})},
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=60,
    )


def test_help_prints_usage_and_exits(tmp_path: Path) -> None:
    result = run("--help", cwd=tmp_path)
    assert result.returncode == 0
    assert result.stdout.startswith("usage: ethercat-mcp")
    assert "ETHERCAT_WRITES_ENABLED" in result.stdout and "sudo" in result.stdout
    assert "jsonrpc" not in result.stdout


def test_version(tmp_path: Path) -> None:
    result = run("--version", cwd=tmp_path)
    assert result.returncode == 0
    assert result.stdout.strip() == f"ethercat-mcp {version('ethercat-mcp')}"


def config_and_stderr(tmp_path: Path, *args: str, env: dict[str, str] | None = None) -> tuple[dict, str]:
    result = run("--print-config", *args, cwd=tmp_path, env=env)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout), result.stderr


def config(tmp_path: Path, *args: str, env: dict[str, str] | None = None) -> dict:
    return config_and_stderr(tmp_path, *args, env=env)[0]


def test_dotenv_in_the_working_directory_reaches_connection_settings(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text("ETHERCAT_INTERFACE=enp9s0\nETHERCAT_CYCLE_TIME=4000\nETHERCAT_PD_WATCHDOG_MS=50\n")
    cfg = config(tmp_path)
    assert cfg["interface"] == "enp9s0"
    assert cfg["cycle_time_us"] == 4000
    assert cfg["pd_watchdog_ms"] == 50
    assert cfg["env_file"] == str(tmp_path / ".env")


def test_implicit_dotenv_cannot_turn_gates_on(tmp_path: Path) -> None:
    """Suite rule 3: the MCP client's working directory may be an untrusted project."""
    (tmp_path / ".env").write_text(
        "ETHERCAT_WRITES_ENABLED=true\nETHERCAT_STATE_CHANGE_ENABLED=true\nETHERCAT_INTERFACE=enp9s0\n"
    )
    cfg, stderr = config_and_stderr(tmp_path)
    assert cfg["writes_enabled"] is False
    assert cfg["state_change_enabled"] is False
    assert cfg["interface"] == "enp9s0"  # connection settings still apply
    for name in ("ETHERCAT_WRITES_ENABLED", "ETHERCAT_STATE_CHANGE_ENABLED"):
        assert f"Ignoring {name} from {tmp_path / '.env'}" in stderr


def test_gates_from_the_environment_or_an_explicit_env_file_are_honoured(tmp_path: Path) -> None:
    assert config(tmp_path, env={"ETHERCAT_WRITES_ENABLED": "true"})["writes_enabled"] is True
    explicit = tmp_path / "bench.env"
    explicit.write_text("ETHERCAT_WRITES_ENABLED=true\nETHERCAT_STATE_CHANGE_ENABLED=true\n")
    cfg = config(tmp_path, "--env-file", str(explicit))
    assert cfg["writes_enabled"] is True and cfg["state_change_enabled"] is True


def test_dotenv_in_a_parent_directory_is_ignored(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text("ETHERCAT_WRITES_ENABLED=true\n")
    child = tmp_path / "child"
    child.mkdir()
    cfg = config(child)
    assert cfg["writes_enabled"] is False
    assert cfg["env_file"] is None


def test_environment_wins_over_dotenv(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text("ETHERCAT_INTERFACE=from-dotenv\n")
    assert config(tmp_path, env={"ETHERCAT_INTERFACE": "from-env"})["interface"] == "from-env"


def test_env_file_option(tmp_path: Path) -> None:
    settings = tmp_path / "bench.env"
    settings.write_text("ETHERCAT_BACKEND=mock\nETHERCAT_MOCK_PORT=16700\n")
    cfg = config(tmp_path, "--env-file", str(settings))
    assert cfg["backend"] == "mock" and cfg["mock_port"] == 16700
    missing = run("--env-file", str(tmp_path / "nope.env"), cwd=tmp_path)
    assert missing.returncode == 2 and "does not exist" in missing.stderr


def test_bad_configuration_fails_at_startup(tmp_path: Path) -> None:
    result = run(cwd=tmp_path, env={"ETHERCAT_WRITES_ENABLED": "sometimes"})
    assert result.returncode == 2
    assert "configuration error: ETHERCAT_WRITES_ENABLED='sometimes' is not a boolean" in result.stderr
    assert result.stdout == ""
