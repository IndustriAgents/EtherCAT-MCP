"""FastMCP wiring for the EtherCAT tools."""

from __future__ import annotations

import logging
import os
import signal
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
from .ec_master import REQUEST_CANCEL_SCOPE, EthercatMaster
from .esi_parser import ESIParser
from .tools import SlaveMap, ToolResources, envelope, register_tools

logger = logging.getLogger(__name__)

# Errors from writing a reply to a client that has already disconnected.
_UNDELIVERABLE = (anyio.ClosedResourceError, anyio.BrokenResourceError, BrokenPipeError, ConnectionResetError)

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

    def request_cancel_scope(self) -> Any:
        """The anyio CancelScope the MCP SDK cancels for notifications/cancelled, or None.

        Private mcp<2 API (RequestContext.session._in_flight[request_id]._cancel_scope),
        hence the guarded lookups; tests/test_mcp_internals.py fails if it moves.
        """
        try:
            context = self._mcp_server.request_context
        except LookupError:
            return None
        in_flight = getattr(getattr(context, "session", None), "_in_flight", None)
        if not isinstance(in_flight, dict):
            return None
        responder = in_flight.get(getattr(context, "request_id", None))
        return getattr(responder, "_cancel_scope", None)

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Sequence[Any] | dict[str, Any]:
        scope = self.request_cancel_scope()
        scope_token = REQUEST_CANCEL_SCOPE.set(scope)
        try:
            result = await self._call_tool(name, arguments)
        finally:
            REQUEST_CANCEL_SCOPE.reset(scope_token)
        if scope is not None and scope.cancel_called:
            # The client cancelled and the SDK has already answered "Request cancelled". A second
            # reply would trip the SDK's "already responded" assertion and can take the server
            # down, so log what happened and end as a real cancellation, which the SDK suppresses.
            outcome = _outcome_of(result)
            logger.warning(
                "Tool %s was cancelled by the client: %s",
                name,
                "nothing was sent"
                if outcome in (None, "not_sent")
                else f"outcome {outcome} (it may have been applied; read back before retrying)",
            )
            await anyio.lowlevel.checkpoint_if_cancelled()
        return result

    async def _call_tool(self, name: str, arguments: dict[str, Any]) -> Sequence[Any] | dict[str, Any]:
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
            meta = {**self._envelope_meta(), "tool": name}
            annotations = tool.annotations
            if annotations is None or not annotations.readOnlyHint:
                # A device-changing tool that failed here never sent anything.
                meta.update({"outcome": "not_sent", "request_sent": False})
            return tool.fn_metadata.convert_result(envelope(False, error=message, meta=meta))


def _outcome_of(result: Any) -> str | None:
    """meta.outcome of a converted tool result (content list, or (content, structured)), if any."""
    structured = result[1] if isinstance(result, tuple) and len(result) == 2 else result
    if isinstance(structured, dict):
        meta = structured.get("meta") or structured.get("result", {}).get("meta")
        if isinstance(meta, dict):
            return meta.get("outcome")
    return None


class EthercatMCPServer:
    def __init__(self, config: EthercatConfig, master: EthercatMaster | None = None) -> None:
        self.config = config
        self.master = master or EthercatMaster(config)
        self.slave_map = SlaveMap(config.slave_map_path)
        self.esi_parser = ESIParser(config.esi_base_path, max_bytes=config.esi_max_mb * 1024 * 1024)
        self.client_gone = False
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
            # Shielded, so the bus is left in a defined state even if the server task is
            # cancelled (PysoemBackend.close requests INIT if process data ran).
            with anyio.CancelScope(shield=True):
                await self.master.close()
            logger.info("EtherCAT master closed")

    def run(self, protocol_out: BinaryIO | None = None) -> None:
        """Serve MCP over stdio. ``protocol_out`` replaces stdout for JSON-RPC (see ``guard_stdout``)."""
        anyio.run(self._run_stdio, protocol_out)

    async def _run_stdio(self, protocol_out: BinaryIO | None) -> None:
        async with anyio.create_task_group() as tg:
            tg.start_soon(self._stop_on_signal)
            stdout = anyio.wrap_file(TextIOWrapper(protocol_out, encoding="utf-8")) if protocol_out else None
            stdin = _NotifyOnEof(
                anyio.wrap_file(TextIOWrapper(sys.stdin.buffer, encoding="utf-8")), self._client_disconnected
            )
            lowlevel = self._server._mcp_server
            try:
                async with stdio_server(stdin=stdin, stdout=stdout) as (read_stream, write_stream):
                    await lowlevel.run(read_stream, write_stream, lowlevel.create_initialization_options())
            except BaseExceptionGroup as group:
                # Replies to calls that were still running when the client closed stdin have
                # nowhere to go: a normal end, not a crash. Anything else still raises.
                _, rest = group.split(_UNDELIVERABLE)
                if not self.client_gone or rest is not None:
                    raise
                logger.info("Dropped replies for the disconnected client")
            tg.cancel_scope.cancel()

    def _client_disconnected(self) -> None:
        """stdin reached EOF: no pending call may send anything any more (INIT still follows on close)."""
        self.client_gone = True
        logger.info("MCP client disconnected; stopping pending device requests")
        self.master.shutdown_calls("the MCP client disconnected (stdin closed)")

    async def _stop_on_signal(self) -> None:
        """Turn SIGTERM/SIGHUP (what MCP clients send on shutdown) and SIGINT into an orderly stop.

        Python's default handler would exit without cleanup, leaving a running
        process image to the slave watchdogs. Here the master is closed first
        (PysoemBackend.close requests INIT if process data ran), then the process
        exits; a normal return would hang on the thread blocked reading stdin.
        """
        try:
            with anyio.open_signal_receiver(signal.SIGTERM, signal.SIGHUP, signal.SIGINT) as signals:
                async for signum in signals:
                    logger.info("Received %s, closing the EtherCAT master", signal.Signals(signum).name)
                    with anyio.CancelScope(shield=True):
                        await self.master.close()
                    logger.info("EtherCAT master closed")
                    sys.stderr.flush()
                    os._exit(0)
        except (NotImplementedError, AttributeError):  # pragma: no cover - Windows has no SIGHUP/signal receiver
            await anyio.sleep_forever()


class _NotifyOnEof:
    """Wraps the stdin stream and calls ``on_eof`` when the client closes it.

    FastMCP stops reading on EOF but lets in-flight tool handlers finish. Without
    this, a write still queued behind the backend lock would be sent after the
    client is gone; ``on_eof`` cancels every pending call's token instead.
    """

    def __init__(self, inner: Any, on_eof: Callable[[], None]) -> None:
        self._inner = inner
        self._on_eof = on_eof

    def __aiter__(self) -> AsyncIterator[str]:
        return self._lines()

    async def _lines(self) -> AsyncIterator[str]:
        try:
            async for line in self._inner:
                yield line
        finally:
            self._on_eof()


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
