"""The FastMCP internals server.py relies on (why pyproject pins mcp < 2).

If an mcp release moves any of these, this file fails with a message naming
the attribute, instead of the server breaking in subtle ways.
"""

from __future__ import annotations

import inspect

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.server.stdio import stdio_server


def test_fastmcp_internals_used_by_the_server_exist() -> None:
    server = FastMCP(name="probe")

    @server.tool()
    def probe(x: int) -> dict:
        """Probe tool."""
        return {"x": x}

    lowlevel = getattr(server, "_mcp_server", None)
    assert lowlevel is not None, "FastMCP._mcp_server is gone (server.py sets its version and runs it)"
    assert hasattr(lowlevel, "version"), "lowlevel Server.version is gone (serverInfo.version)"
    assert callable(getattr(lowlevel, "run", None)), "lowlevel Server.run is gone"
    assert callable(getattr(lowlevel, "create_initialization_options", None))
    manager = getattr(server, "_tool_manager", None)
    assert manager is not None, "FastMCP._tool_manager is gone (EnvelopeFastMCP.call_tool)"
    tool = manager.get_tool("probe")
    assert tool is not None, "ToolManager.get_tool is gone"
    assert callable(getattr(tool.fn_metadata, "convert_result", None)), "FuncMetadata.convert_result is gone"
    assert "stdout" in inspect.signature(stdio_server).parameters, "stdio_server(stdout=...) is gone"
    assert "call_tool" in vars(FastMCP), "FastMCP.call_tool is no longer the hook EnvelopeFastMCP overrides"
    assert issubclass(ToolError, Exception)


def test_argument_validation_errors_still_reach_call_tool_as_tool_errors() -> None:
    """EnvelopeFastMCP turns ToolError(cause=ValidationError) into an envelope."""
    import anyio
    from pydantic import ValidationError

    server = FastMCP(name="probe")

    @server.tool()
    def probe(x: int) -> dict:
        """Probe tool."""
        return {"x": x}

    async def call() -> BaseException | None:
        try:
            await server.call_tool("probe", {"x": "not an int"})
        except ToolError as exc:
            return exc.__cause__
        return None

    cause = anyio.run(call)
    assert isinstance(cause, ValidationError), f"argument errors now surface as {type(cause).__name__}"
