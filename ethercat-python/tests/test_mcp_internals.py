"""The FastMCP internals server.py relies on (why pyproject pins mcp < 2).

If an mcp release moves any of these, this file fails with a message naming
the attribute, instead of the server breaking in subtle ways.
"""

from __future__ import annotations

import inspect
from importlib.metadata import version

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


def test_request_cancel_scope_internals_exist() -> None:
    """EnvelopeFastMCP.request_cancel_scope reads RequestContext.session._in_flight[request_id]._cancel_scope.

    The SDK sets that scope's cancel_called synchronously when the client cancels, before
    CancelledError reaches the tool; CallToken.begin_send relies on it (the cancel race).
    """
    import inspect

    from mcp.shared.context import RequestContext
    from mcp.shared.session import BaseSession, RequestResponder

    assert "_cancel_scope" in inspect.getsource(RequestResponder.__init__), "RequestResponder._cancel_scope moved"
    assert "self._cancel_scope.cancel()" in inspect.getsource(RequestResponder.cancel)
    assert "_in_flight" in inspect.getsource(BaseSession.__init__), "BaseSession._in_flight moved"
    fields = getattr(RequestContext, "__dataclass_fields__", {})
    assert {"request_id", "session"} <= set(fields), "RequestContext no longer has request_id/session"


def test_pysoem_runtime_includes_released_gil_sdo_length_fix():
    # 1.1.12 loses native size_inout in its GIL-releasing helper and returns
    # allocation capacity. Dependency resolution must never restore that floor.
    installed = tuple(int(part) for part in version("pysoem").split(".")[:3])
    assert installed >= (1, 1, 13), "PySOEM 1.1.13+ is required for exact-length SDO uploads"
