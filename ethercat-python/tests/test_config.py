"""Configuration is read at startup, after .env, with safe defaults (known issues 3 and 4)."""

from __future__ import annotations

import pytest

from ethercat_mcp.config import ConfigError, EthercatConfig


def test_defaults_are_read_only() -> None:
    config = EthercatConfig.from_env({})
    assert config.writes_enabled is False
    assert config.state_change_enabled is False
    assert config.backend == "pysoem"
    assert config.interface == "eth0"


def test_environment_is_read_when_called_not_at_import(monkeypatch: pytest.MonkeyPatch) -> None:
    import ethercat_mcp.config as config_module
    import ethercat_mcp.server  # noqa: F401 - import everything the CLI imports first

    # Set after import, as cli.main() does after load_dotenv().
    monkeypatch.setenv("ETHERCAT_INTERFACE", "enp3s0")
    monkeypatch.setenv("ETHERCAT_WRITES_ENABLED", "true")
    monkeypatch.setenv("ETHERCAT_STATE_CHANGE_ENABLED", "yes")
    monkeypatch.setenv("ETHERCAT_CYCLE_TIME", "2000")
    config = config_module.EthercatConfig.from_env()
    assert config.interface == "enp3s0"
    assert config.writes_enabled is True
    assert config.state_change_enabled is True
    assert config.cycle_time_us == 2000


def test_master_and_tools_share_one_gate() -> None:
    from ethercat_mcp.ec_master import EthercatMaster

    config = EthercatConfig.from_env({"ETHERCAT_WRITES_ENABLED": "false"})
    master = EthercatMaster(config)
    assert master.config is config
    assert master.connection_status()["writes_enabled"] is False


@pytest.mark.parametrize(
    ("env", "message"),
    [
        ({"ETHERCAT_WRITES_ENABLED": "maybe"}, "not a boolean"),
        ({"ETHERCAT_STATE_CHANGE_ENABLED": "ture"}, "not a boolean"),
        ({"ETHERCAT_CYCLE_TIME": "fast"}, "not an integer"),
        ({"ETHERCAT_CYCLE_TIME": "10"}, "out of range"),
        ({"ETHERCAT_BACKEND": "soem"}, "not supported"),
        ({"ETHERCAT_MOCK_PORT": "70000"}, "out of range"),
        ({"ETHERCAT_INTERFACE": "  "}, "empty"),
    ],
)
def test_unusable_values_fail(env: dict[str, str], message: str) -> None:
    with pytest.raises(ConfigError, match=message):
        EthercatConfig.from_env(env)


def test_mock_backend_settings() -> None:
    config = EthercatConfig.from_env(
        {"ETHERCAT_BACKEND": "MOCK", "ETHERCAT_MOCK_HOST": "localhost", "ETHERCAT_MOCK_PORT": "16700"}
    )
    assert config.is_mock
    assert (config.mock_host, config.mock_port) == ("localhost", 16700)
