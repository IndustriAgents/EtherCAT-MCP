"""CLI entry point for the EtherCAT MCP server."""

from __future__ import annotations

import argparse
import dataclasses
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any

from dotenv import dotenv_values, load_dotenv

from ._version import __version__
from .config import OWN_EXTRA_VARIABLES, PROTECTED_VARIABLES, ConfigError, EthercatConfig

logger = logging.getLogger("ethercat_mcp")

ENV_HELP = """\
settings (environment variables, or a .env file):
  ETHERCAT_BACKEND               pysoem (real bus, default) or mock (the JSON mock slave)
  ETHERCAT_INTERFACE             network interface PySOEM opens (default eth0)
  ETHERCAT_CYCLE_TIME            process-data cycle in microseconds (default 1000)
  ETHERCAT_TIMEOUT               state-change and mock-request timeout in microseconds (default 500000)
  ETHERCAT_EXPECTED_WKC          working counter process data must reach (default 0 = the group's own
                                 expected count when every process-data slave is up, else > 0)
  ETHERCAT_PD_WATCHDOG_MS        process-data watchdog set on slaves with outputs (default 100)
  ETHERCAT_WRITES_ENABLED        allow write_pdo/write_sdo/write_slave_by_alias (default false)
  ETHERCAT_STATE_CHANGE_ENABLED  with ETHERCAT_WRITES_ENABLED, allow set_slave_state (default false)
  ETHERCAT_MOCK_HOST / _PORT     mock slave address for ETHERCAT_BACKEND=mock (default 127.0.0.1:6700)
  SLAVE_MAP_FILE                 JSON file of slave aliases
  ETHERCAT_ESI_PATH              directory searched for ESI files
  ETHERCAT_ESI_MAX_MB            largest ESI file load_esi_file parses (default 16)

The only .env read automatically is ethercat-python/.env of a source checkout
(never the working directory or a parent; none for an installed package).
Variables already set in the environment win over it, and it can only set
ETHERCAT_* and SLAVE_MAP_FILE, never ETHERCAT_WRITES_ENABLED,
ETHERCAT_STATE_CHANGE_ENABLED, ETHERCAT_PD_WATCHDOG_MS or ETHERCAT_EXPECTED_WKC:
those come only from the real environment or --env-file.
sudo drops the caller's environment, so under sudo put the settings on the
command line (sudo ETHERCAT_INTERFACE=eth0 .venv/bin/ethercat-mcp) or use
--env-file.
"""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ethercat-mcp",
        description="EtherCAT MCP server. Speaks MCP (JSON-RPC) over stdio; start it from an MCP client.",
        epilog=ENV_HELP,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument(
        "--env-file",
        type=Path,
        metavar="PATH",
        help="load settings from PATH instead of the project's own .env; unlike that file, "
        "it may also set the write and state-change gates and the safety limits",
    )
    parser.add_argument(
        "--print-config",
        action="store_true",
        help="print the effective configuration as JSON and exit (useful to check what sudo passed through)",
    )
    return parser


def project_env_file() -> Path | None:
    """The only .env the server loads by itself: ``ethercat-python/.env`` of a source checkout.

    It is found from this file's location (``ethercat-python/src/ethercat_mcp``), never
    from the working directory or a parent directory (suite rule 3c). An installed
    package (site-packages) has no implicit .env at all.
    """
    package = Path(__file__).resolve().parent
    project = package.parent.parent
    if package.parent.name == "src" and (project / "pyproject.toml").is_file():
        return project / ".env"
    return None


def load_env_file(env_file: Path | None) -> Path | None:
    """Load ``--env-file``, or else the project's own ``.env`` (see ``project_env_file``).

    An explicit ``--env-file`` is trusted like the environment. The implicit ``.env``
    may only set this server's own settings (``ETHERCAT_*`` and ``SLAVE_MAP_FILE``),
    and never the safety switches or the settings that weaken a safety check
    (``PROTECTED_VARIABLES``). Keys are compared case-insensitively, as Windows
    upper-cases environment variable names. Anything else is ignored with a warning.
    """
    if env_file is not None:
        path = env_file.expanduser()
        if not path.is_file():
            raise ConfigError(f"--env-file {env_file} does not exist")
        load_dotenv(path, override=False)
        return path
    candidate = project_env_file()
    if candidate is None or not candidate.is_file():
        return None
    for key, value in dotenv_values(candidate).items():  # handles "export " prefixes and quotes
        if value is None:
            continue
        name = key.strip().upper()
        if name in PROTECTED_VARIABLES:
            logger.warning(
                "Ignoring %s from %s: safety switches and safety limits are only read from the process "
                "environment or --env-file",
                key,
                candidate,
            )
            continue
        if not (name.startswith("ETHERCAT_") or name in OWN_EXTRA_VARIABLES):
            logger.warning("Ignoring %s from %s: it is not an ethercat-mcp setting", key, candidate)
            continue
        os.environ.setdefault(name, value)
    return candidate


def config_summary(config: EthercatConfig, env_file: Path | None = None) -> dict[str, Any]:
    summary = {k: (str(v) if isinstance(v, Path) else v) for k, v in dataclasses.asdict(config).items()}
    summary["env_file"] = str(env_file) if env_file else None
    summary["version"] = __version__
    return summary


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO, stream=sys.stderr, format="%(levelname)s %(name)s: %(message)s")
    try:
        env_file = load_env_file(args.env_file)
        config = EthercatConfig.from_env()
    except ConfigError as exc:
        print(f"ethercat-mcp: configuration error: {exc}", file=sys.stderr)
        sys.exit(2)

    if args.print_config:
        print(json.dumps(config_summary(config, env_file), indent=2))
        return

    # Imported here so --help, --version and --print-config stay fast.
    from .server import EthercatMCPServer, guard_stdout

    protocol_out = guard_stdout()
    if config.is_mock:
        logger.warning(
            "ETHERCAT_BACKEND=mock: tools answer from the mock slave at %s:%s, NOT from a real EtherCAT bus",
            config.mock_host,
            config.mock_port,
        )
    logger.info(
        "ethercat-mcp %s starting (backend=%s, interface=%s, writes=%s, state changes=%s, env file=%s)",
        __version__,
        config.backend,
        config.interface,
        "enabled" if config.writes_enabled else "disabled",
        "enabled" if config.state_change_enabled else "disabled",
        env_file or "none",
    )
    # The lifespan closes the master on a normal exit, and the signal handler on SIGTERM,
    # SIGHUP and SIGINT; both are bounded so that a stuck device call cannot block shutdown.
    try:
        server = EthercatMCPServer(config)
    except (ValueError, OSError) as exc:
        print(f"ethercat-mcp: configuration error: {exc}", file=sys.stderr)
        raise SystemExit(2) from None
    try:
        server.run(protocol_out)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
