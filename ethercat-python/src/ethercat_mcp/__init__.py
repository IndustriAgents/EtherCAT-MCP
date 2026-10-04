"""EtherCAT MCP server package."""

from typing import Any

from ._version import __version__

__all__ = ["EthercatMCPServer", "__version__"]


def __getattr__(name: str) -> Any:
    # Lazy, so `ethercat-mcp --help` does not import the MCP SDK.
    if name == "EthercatMCPServer":
        from .server import EthercatMCPServer

        return EthercatMCPServer
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
