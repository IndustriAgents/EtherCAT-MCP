"""Contract-driven MCP boundary. This module is owned by this protocol package."""

from __future__ import annotations

import json
from importlib.resources import files
from typing import Any

from jsonschema import Draft202012Validator
from mcp.types import CallToolResult, TextContent, Tool

from ..application.execution import Execution


def envelope_of(result: Any) -> dict[str, Any] | None:
    if isinstance(result, CallToolResult):
        if isinstance(result.structuredContent, dict):
            return result.structuredContent
        result = result.content
    if isinstance(result, tuple):
        content, structured = result
        if isinstance(structured, dict):
            return structured
        result = content
    if isinstance(result, dict):
        return result
    if isinstance(result, (list, tuple)) and result:
        try:
            return json.loads(result[0].text)
        except (AttributeError, TypeError, ValueError):
            return None
    return None


def as_result(value: dict[str, Any]) -> tuple:
    return ([TextContent(type="text", text=json.dumps(value))], value)


class ContractBoundary:
    def __init__(self, server: Any, policy=None):
        package = __package__.split(".")[0]
        self.contract = json.loads(files(package).joinpath("_contract/tools.json").read_text())
        self.original_call = server.call_tool
        self.tools = {t["name"]: t for t in self.contract["tools"]}
        self.validators = {n: Draft202012Validator(t["inputSchema"]) for n, t in self.tools.items()}
        self.legacy_validators = {n: Draft202012Validator(s) for n, s in self.contract["legacySchemas"].items()}
        self.execution = Execution.from_environment(self.contract["protocol"], policy)
        self.server = server

    async def list_tools(self):
        return [Tool(**t) for t in self.tools.values()]

    def refusal(self, name, reason, code="invalid_arguments"):
        canonical = self.contract["legacy"].get(name, name)
        write = not self.tools.get(canonical, {}).get("annotations", {}).get("readOnlyHint", False)
        meta = {"tool": name, "code": code}
        if write:
            meta.update(outcome="not_sent", request_sent=False)
        return as_result({"success": False, "data": None, "error": reason, "meta": meta})

    async def call_tool(self, name, arguments=None):
        arguments = {} if arguments is None else arguments
        route = self.contract["routes"].get(name)
        canonical = self.contract["legacy"].get(name, name)
        handler, forwarded = name, arguments
        legacy_call = route is None
        validator = self.legacy_validators.get(name)
        if route:
            canonical = name
            validator = self.validators[name]
            if "selector" in route:
                selector = route["selector"]
                if isinstance(arguments, dict) and selector in arguments:
                    handler = (
                        route["options"].get(arguments[selector]) if isinstance(arguments[selector], str) else None
                    )
                    forwarded = {k: v for k, v in arguments.items() if k != selector}
                elif "legacyHandler" in route:
                    legacy_call = True
                    handler = route["legacyHandler"]
                    validator = self.legacy_validators[handler]
                else:
                    handler = None
            else:
                legacy_call = True
                handler = route["handler"]
        if validator is None:
            from mcp.server.fastmcp.exceptions import ToolError

            raise ToolError(f"Unknown tool: {name}")
        if not isinstance(arguments, dict):
            return self.refusal(name, "Arguments must be an object")
        try:
            encoded = json.dumps(arguments, allow_nan=False).encode()
        except (ValueError, TypeError):
            return self.refusal(name, "Arguments must contain finite JSON values")
        if len(encoded) > self.execution.policy.max_request_bytes:
            return self.refusal(name, "Request exceeds max_request_bytes", "resource_limit")
        error = None if legacy_call else next(validator.iter_errors(arguments), None)
        if error is not None or handler is None:
            # Do not echo argument values: they may contain credentials or payloads.
            path = ".".join(map(str, error.absolute_path)) if error else ""
            if error and error.validator == "additionalProperties":
                path = "Unknown argument(s): " + ", ".join(
                    sorted(set(arguments) - set(error.schema.get("properties", {})))
                )
            if error and error.validator == "required":
                path = ", ".join(k for k in error.validator_value if k not in arguments)
            expected = error.validator_value if error and error.validator == "type" else "documented shape"
            return self.refusal(
                name,
                (
                    "Invalid arguments: "
                    f"{path or 'arguments'}"
                    ": "
                    f"{(error.validator if error else 'selector')}"
                    " constraint failed (expected "
                    f"{expected}"
                    ")"
                ),
            )
        definition = self.tools[canonical]
        write = not definition.get("annotations", {}).get("readOnlyHint", False)
        local = canonical in self.contract["localStatusTools"]
        try:
            ticket = self.execution.admit(canonical, handler, write=write, local=local)
        except (ValueError, OSError) as exc:
            return self.refusal(name, str(exc), "policy_denied")
        result = None
        try:
            result = await self.original_call(handler, forwarded)
            return result
        finally:
            # Audit the observed result; never turn an audit failure after sending
            # into a not_sent result, and never repeat an uncertain operation.
            self.execution.finish(ticket, envelope_of(result))

    async def wire_call(self, name, arguments=None):
        result = await self.call_tool(name, arguments)
        value = envelope_of(result)
        if value is None:
            return result
        return CallToolResult(
            content=[TextContent(type="text", text=json.dumps(value))],
            structuredContent=value,
            isError=value.get("success") is False,
        )


def install_contract(server, policy=None):
    boundary = ContractBoundary(server, policy)
    server.contract_boundary = boundary
    server.list_tools = boundary.list_tools
    server.call_tool = boundary.call_tool
    # FastMCP binds callbacks at construction; update only here at the SDK edge.
    server._mcp_server.list_tools()(boundary.list_tools)
    server._mcp_server.call_tool(validate_input=False)(boundary.wire_call)
    return boundary
