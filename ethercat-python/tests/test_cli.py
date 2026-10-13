"""CLI arguments and .env handling (known issues 4 and 10; review finding 3; suite rule 3)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from importlib.metadata import version
from pathlib import Path

import pytest

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


def test_dotenv_in_the_working_directory_is_not_read(tmp_path: Path) -> None:
    """Suite rule 3c: the working directory may be the MCP client's (untrusted) project."""
    (tmp_path / ".env").write_text("ETHERCAT_INTERFACE=from-cwd\nETHERCAT_MOCK_HOST=evil.example\n")
    cfg = config(tmp_path)
    assert cfg["interface"] != "from-cwd" and cfg["mock_host"] != "evil.example"
    assert cfg["env_file"] != str(tmp_path / ".env")


def test_dotenv_in_a_parent_directory_is_not_read(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text("ETHERCAT_INTERFACE=from-parent\nETHERCAT_WRITES_ENABLED=true\n")
    child = tmp_path / "child"
    child.mkdir()
    cfg = config(child)
    assert cfg["interface"] != "from-parent"
    assert cfg["writes_enabled"] is False


def test_the_implicit_dotenv_is_the_projects_own() -> None:
    from ethercat_mcp import cli

    assert cli.project_env_file() == Path(cli.__file__).resolve().parents[2] / ".env"
    assert cli.project_env_file().parent.name == "ethercat-python"


def test_an_installed_package_has_no_implicit_dotenv(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from ethercat_mcp import cli

    site = tmp_path / "lib" / "python3.11" / "site-packages" / "ethercat_mcp"
    site.mkdir(parents=True)
    (tmp_path / "lib" / "pyproject.toml").write_text("[project]\nname = 'decoy'\n")
    monkeypatch.setattr(cli, "__file__", str(site / "cli.py"))
    assert cli.project_env_file() is None


@pytest.fixture
def project_dotenv(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the implicit .env at a temporary file and keep os.environ changes local."""
    from ethercat_mcp import cli

    path = tmp_path / ".env"
    monkeypatch.setattr(cli, "project_env_file", lambda: path)
    monkeypatch.setattr(os, "environ", dict(os.environ))
    return path


def load(path: Path, text: str, caplog: pytest.LogCaptureFixture) -> tuple[dict[str, str], str]:
    from ethercat_mcp import cli

    path.write_text(text)
    with caplog.at_level("WARNING", logger="ethercat_mcp"):
        assert cli.load_env_file(None) == path
    return os.environ, caplog.text


def test_project_dotenv_sets_connection_settings(project_dotenv: Path, caplog: pytest.LogCaptureFixture) -> None:
    from ethercat_mcp.config import EthercatConfig

    environ, _ = load(
        project_dotenv,
        "ethercat_interface=enp9s0\nexport ETHERCAT_CYCLE_TIME=4000\nSLAVE_MAP_FILE='/maps/slaves.json'\n",
        caplog,
    )
    config = EthercatConfig.from_env(environ)
    assert config.interface == "enp9s0"  # keys are normalised to upper case
    assert config.cycle_time_us == 4000
    assert str(config.slave_map_path) == "/maps/slaves.json"


@pytest.mark.parametrize(
    "line",
    [
        "ETHERCAT_WRITES_ENABLED=true",
        "ethercat_writes_enabled=true",
        "Ethercat_Writes_Enabled='true'",
        'export ETHERCAT_WRITES_ENABLED="true"',
        "  ETHERCAT_STATE_CHANGE_ENABLED = true",
        "ethercat_state_change_enabled=1",
        "ETHERCAT_PD_WATCHDOG_MS=6000",
        "ethercat_expected_wkc=1",
    ],
)
def test_project_dotenv_cannot_set_gates_or_safety_limits(
    project_dotenv: Path, caplog: pytest.LogCaptureFixture, line: str
) -> None:
    """Suite rules 3 and 3a, and the review decision on the watchdog and working-counter limits."""
    from ethercat_mcp.config import EthercatConfig

    environ, log = load(project_dotenv, line + "\n", caplog)
    name = line.split("=", 1)[0].replace("export", "").strip().upper()
    assert name not in environ and name.lower() not in environ
    assert "Ignoring" in log and "safety switches and safety limits" in log
    config = EthercatConfig.from_env(environ)
    assert config.writes_enabled is False and config.state_change_enabled is False
    assert config.pd_watchdog_ms == 100 and config.expected_wkc == 0


@pytest.mark.parametrize(
    "line", ["PATH=/tmp/evil", "LD_PRELOAD=/tmp/x.so", "PYTHONPATH=/tmp", "DYLD_INSERT_LIBRARIES=x"]
)
def test_project_dotenv_only_sets_this_servers_variables(
    project_dotenv: Path, caplog: pytest.LogCaptureFixture, line: str
) -> None:
    """Suite rule 3b."""
    name, value = line.split("=", 1)
    before = os.environ.get(name)
    environ, log = load(project_dotenv, line + "\n", caplog)
    assert environ.get(name) == before != value
    assert f"Ignoring {name}" in log and "not an ethercat-mcp setting" in log


def test_gates_from_the_environment_or_an_explicit_env_file_are_honoured(tmp_path: Path) -> None:
    assert config(tmp_path, env={"ETHERCAT_WRITES_ENABLED": "true"})["writes_enabled"] is True
    explicit = tmp_path / "bench.env"
    explicit.write_text(
        "ETHERCAT_WRITES_ENABLED=true\nETHERCAT_STATE_CHANGE_ENABLED=true\nETHERCAT_PD_WATCHDOG_MS=50\n"
    )
    cfg = config(tmp_path, "--env-file", str(explicit))
    assert cfg["writes_enabled"] is True and cfg["state_change_enabled"] is True and cfg["pd_watchdog_ms"] == 50


def test_environment_wins_over_the_project_dotenv(project_dotenv: Path, caplog: pytest.LogCaptureFixture) -> None:
    os.environ["ETHERCAT_INTERFACE"] = "from-env"
    environ, _ = load(project_dotenv, "ETHERCAT_INTERFACE=from-dotenv\n", caplog)
    assert environ["ETHERCAT_INTERFACE"] == "from-env"


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
