"""FastMCP wiring for the EtherCAT tools."""

from __future__ import annotations

import logging
import os
import sys
from collections.abc import AsyncIterator, Callable, Sequence
from contextlib import asynccontextmanager
from io import TextIOWrapper
from typing import Any, BinaryIO

import anyio
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.server.stdio import stdio_server
from pydantic import ValidationError

from ._version import __version__
from .config import EthercatConfig
from .ec_master import EthercatMaster
from .esi_parser import ESIParser
from .tools import SlaveMap, ToolResources, envelope, register_tools

logger = logging.getLogger(__name__)

INSTRUCTIONS = (
    "EtherCAT master tools. Every result is an envelope {success, data, error, meta}; meta.backend says "
    "whether a real bus (pysoem) or the simulator (mock, meta.simulated=true) answered. Call scan_network "
    "before other bus tools. Writes and state changes are refused unless the server was started with "
    "ETHERCAT_WRITES_ENABLED / ETHERCAT_STATE_CHANGE_ENABLED set to true."
)


class EnvelopeFastMCP(FastMCP):
    """FastMCP that reports bad arguments and unexpected errors as envelopes.

    FastMCP validates arguments against the tool signature before the tool runs,
    and would otherwise answer a wrong type (for example ``slave_position="x"``)
    with a bare ``Error executing tool ...`` result instead of the envelope.
    """

    def __init__(self, *args: Any, envelope_meta: Callable[[], dict[str, Any]], **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._envelope_meta = envelope_meta

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Sequence[Any] | dict[str, Any]:
        try:
            return await super().call_tool(name, arguments)
        except ToolError as exc:
            tool = self._tool_manager.get_tool(name)
            if tool is None:
                raise
            cause = exc.__cause__
            if isinstance(cause, ValidationError):
                problems = "; ".join(
                    f"{'.'.join(str(part) for part in err['loc']) or 'arguments'}: {err['msg']}"
                    for err in cause.errors()
                )
                message = f"Invalid arguments for {name}: {problems}"
            else:
                logger.exception("Unexpected error in tool %s", name)
                message = f"Unexpected error in {name}: {cause or exc}"
            result = envelope(False, error=message, meta={**self._envelope_meta(), "tool": name})
            return tool.fn_metadata.convert_result(result)


class EthercatMCPServer:
    def __init__(self, config: EthercatConfig, master: EthercatMaster | None = None) -> None:
        self.config = config
        self.master = master or EthercatMaster(config)
        self.slave_map = SlaveMap(config.slave_map_path)
        self.esi_parser = ESIParser(config.esi_base_path)
        self._server = EnvelopeFastMCP(
            name="EtherCAT MCP Server",
            instructions=INSTRUCTIONS,
            dependencies=["pysoem"],
            lifespan=self._lifespan,
            envelope_meta=self.master.describe,
        )
        # FastMCP has no version argument; without this, serverInfo.version is the mcp SDK's version.
        self._server._mcp_server.version = __version__
        register_tools(
            self._server,
            ToolResources(master=self.master, config=config, slave_map=self.slave_map, esi_parser=self.esi_parser),
        )

    @property
    def mcp(self) -> EnvelopeFastMCP:
        return self._server

    @asynccontextmanager
    async def _lifespan(self, server: FastMCP) -> AsyncIterator[None]:  # noqa: ARG002
        # The interface is opened lazily by the first tool that needs the bus, so the
        # server starts without EtherCAT hardware or raw-socket privileges.
        try:
            yield None
        finally:
            await self.master.close()

    def run(self, protocol_out: BinaryIO | None = None) -> None:
        """Serve MCP over stdio. ``protocol_out`` replaces stdout for JSON-RPC (see ``guard_stdout``)."""
        anyio.run(self._run_stdio, protocol_out)

    async def _run_stdio(self, protocol_out: BinaryIO | None) -> None:
        stdout = anyio.wrap_file(TextIOWrapper(protocol_out, encoding="utf-8")) if protocol_out else None
        lowlevel = self._server._mcp_server
        async with stdio_server(stdout=stdout) as (read_stream, write_stream):
            await lowlevel.run(read_stream, write_stream, lowlevel.create_initialization_options())


def guard_stdout() -> BinaryIO:
    """Keep stdout for JSON-RPC only.

    Duplicates the real stdout to a private descriptor for the MCP transport, then
    points file descriptor 1 and ``sys.stdout`` at stderr. A stray ``print()`` or
    a C library writing to stdout (SOEM can, in debug builds) then lands in stderr
    instead of corrupting the protocol stream.
    """
    sys.stdout.flush()
    protocol_fd = os.dup(1)
    os.dup2(2, 1)
    sys.stdout = sys.stderr
    return os.fdopen(protocol_fd, "wb")
