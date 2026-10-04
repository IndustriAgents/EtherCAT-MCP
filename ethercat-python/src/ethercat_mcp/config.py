"""Server configuration, read from the environment at startup.

Nothing in this module reads the environment at import time. ``cli.main()``
loads the ``.env`` file first and only then calls :meth:`EthercatConfig.from_env`,
so values from ``.env`` and from the real environment are treated the same way.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

BACKENDS = ("pysoem", "mock")

# Safety switches. Suite rule: they are honoured only from the real process environment or an
# explicit --env-file, never from a .env file found automatically (see cli.load_env_file).
GATE_VARIABLES = ("ETHERCAT_WRITES_ENABLED", "ETHERCAT_STATE_CHANGE_ENABLED")

_TRUE = {"1", "true", "yes", "y", "on"}
_FALSE = {"0", "false", "no", "n", "off", ""}


class ConfigError(ValueError):
    """Raised when an environment variable holds an unusable value."""


def _get(env: Mapping[str, str], name: str, default: str) -> str:
    value = env.get(name)
    return default if value is None else value.strip()


def _bool(env: Mapping[str, str], name: str, default: bool) -> bool:
    raw = env.get(name)
    if raw is None:
        return default
    value = raw.strip().lower()
    if value in _TRUE:
        return True
    if value in _FALSE:
        return False
    raise ConfigError(f"{name}={raw!r} is not a boolean (use true or false)")


def _int(env: Mapping[str, str], name: str, default: int, minimum: int, maximum: int | None = None) -> int:
    raw = _get(env, name, str(default))
    try:
        value = int(raw, 0)
    except ValueError:
        raise ConfigError(f"{name}={raw!r} is not an integer") from None
    if value < minimum or (maximum is not None and value > maximum):
        upper = f" and {maximum}" if maximum is not None else ""
        raise ConfigError(f"{name}={value} is out of range (expected {minimum}{upper or ' or more'})")
    return value


def _path(env: Mapping[str, str], name: str) -> Path | None:
    raw = _get(env, name, "")
    return Path(raw).expanduser() if raw else None


@dataclass(frozen=True, slots=True)
class EthercatConfig:
    """Every setting the server uses. Build it with :meth:`from_env`."""

    backend: str = "pysoem"
    interface: str = "eth0"
    cycle_time_us: int = 1000
    timeout_us: int = 500_000
    expected_wkc: int = 0
    pd_watchdog_ms: int = 100
    writes_enabled: bool = False
    state_change_enabled: bool = False
    slave_map_path: Path | None = None
    esi_base_path: Path | None = None
    mock_host: str = "127.0.0.1"
    mock_port: int = 6700

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> EthercatConfig:
        """Read the configuration from ``environ`` (default: ``os.environ``).

        Raises :class:`ConfigError` for values that cannot be used, so the
        server fails at startup instead of at the first tool call.
        """
        env = os.environ if environ is None else environ
        backend = _get(env, "ETHERCAT_BACKEND", "pysoem").lower()
        if backend not in BACKENDS:
            raise ConfigError(f"ETHERCAT_BACKEND={backend!r} is not supported (use one of: {', '.join(BACKENDS)})")
        interface = _get(env, "ETHERCAT_INTERFACE", "eth0")
        if not interface:
            raise ConfigError("ETHERCAT_INTERFACE is empty")
        mock_host = _get(env, "ETHERCAT_MOCK_HOST", "127.0.0.1")
        if not mock_host:
            raise ConfigError("ETHERCAT_MOCK_HOST is empty")
        cycle_time_us = _int(env, "ETHERCAT_CYCLE_TIME", 1000, minimum=100, maximum=1_000_000)
        pd_watchdog_ms = _int(env, "ETHERCAT_PD_WATCHDOG_MS", 100, minimum=1, maximum=6000)
        if pd_watchdog_ms * 1000 < 3 * cycle_time_us:
            raise ConfigError(
                f"ETHERCAT_PD_WATCHDOG_MS={pd_watchdog_ms} is too short for ETHERCAT_CYCLE_TIME={cycle_time_us} us "
                "(use at least three cycles)"
            )
        return cls(
            backend=backend,
            interface=interface,
            cycle_time_us=cycle_time_us,
            pd_watchdog_ms=pd_watchdog_ms,
            timeout_us=_int(env, "ETHERCAT_TIMEOUT", 500_000, minimum=1000, maximum=60_000_000),
            expected_wkc=_int(env, "ETHERCAT_EXPECTED_WKC", 0, minimum=0, maximum=0xFFFF),
            writes_enabled=_bool(env, "ETHERCAT_WRITES_ENABLED", False),
            state_change_enabled=_bool(env, "ETHERCAT_STATE_CHANGE_ENABLED", False),
            slave_map_path=_path(env, "SLAVE_MAP_FILE"),
            esi_base_path=_path(env, "ETHERCAT_ESI_PATH"),
            mock_host=mock_host,
            mock_port=_int(env, "ETHERCAT_MOCK_PORT", 6700, minimum=1, maximum=65535),
        )

    @property
    def is_mock(self) -> bool:
        return self.backend == "mock"

    @property
    def state_changes_allowed(self) -> bool:
        """State changes need both gates (suite rule: a separate gate never replaces the write gate)."""
        return self.writes_enabled and self.state_change_enabled
