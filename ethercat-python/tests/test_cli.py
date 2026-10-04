"""CLI arguments and .env handling (known issues 4 and 10; review finding 3)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
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
    assert result.stdout.strip() == "ethercat-mcp 0.1.0"


def config(tmp_path: Path, *args: str, env: dict[str, str] | None = None) -> dict:
    result = run("--print-config", *args, cwd=tmp_path, env=env)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_dotenv_in_the_working_directory_reaches_every_setting(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text(
        "ETHERCAT_INTERFACE=enp9s0\nETHERCAT_CYCLE_TIME=4000\nETHERCAT_STATE_CHANGE_ENABLED=true\n"
    )
    cfg = config(tmp_path)
    assert cfg["interface"] == "enp9s0"
    assert cfg["cycle_time_us"] == 4000
    assert cfg["state_change_enabled"] is True
    assert cfg["writes_enabled"] is False
    assert cfg["env_file"] == str(tmp_path / ".env")


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
