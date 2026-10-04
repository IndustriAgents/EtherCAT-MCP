"""CLI entry point for the EtherCAT MCP server."""

from __future__ import annotations

import argparse
import dataclasses
import json
import logging
import sys
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from ._version import __version__
from .config import ConfigError, EthercatConfig

logger = logging.getLogger("ethercat_mcp")

ENV_HELP = """\
settings (environment variables, or a .env file):
  ETHERCAT_BACKEND               pysoem (real bus, default) or mock (the JSON mock slave)
  ETHERCAT_INTERFACE             network interface PySOEM opens (default eth0)
  ETHERCAT_CYCLE_TIME            process-data cycle in microseconds (default 1000)
  ETHERCAT_TIMEOUT               state-change and mock-request timeout in microseconds (default 500000)
  ETHERCAT_EXPECTED_WKC          minimum working counter for write_pdo (default 0 = only require > 0)
  ETHERCAT_WRITES_ENABLED        allow write_pdo/write_sdo/write_slave_by_alias (default false)
  ETHERCAT_STATE_CHANGE_ENABLED  allow set_slave_state (default false)
  ETHERCAT_MOCK_HOST / _PORT     mock slave address for ETHERCAT_BACKEND=mock (default 127.0.0.1:6700)
  SLAVE_MAP_FILE                 JSON file of slave aliases
  ETHERCAT_ESI_PATH              directory searched for ESI files

Variables already set in the environment win over the .env file. sudo drops
the caller's environment, so under sudo put the settings on the command line
(sudo ETHERCAT_INTERFACE=eth0 .venv/bin/ethercat-mcp) or use --env-file.
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
        help="load settings from PATH instead of ./.env (the .env in the current directory)",
    )
    parser.add_argument(
        "--print-config",
        action="store_true",
        help="print the effective configuration as JSON and exit (useful to check what sudo passed through)",
    )
    return parser


def load_env_file(env_file: Path | None) -> Path | None:
    """Load ``env_file``, or ``./.env`` if it exists. Never searches parent directories."""
    if env_file is not None:
        path = env_file.expanduser()
        if not path.is_file():
            raise ConfigError(f"--env-file {env_file} does not exist")
        load_dotenv(path, override=False)
        return path
    candidate = Path.cwd() / ".env"
    if candidate.is_file():
        load_dotenv(candidate, override=False)
        return candidate
    return None


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
    server = EthercatMCPServer(config)
    try:
        server.run(protocol_out)
    except KeyboardInterrupt:
        pass
    finally:
        # Normally done by the lifespan already; close() is idempotent.
        server.master.backend.close()


if __name__ == "__main__":
    main()
