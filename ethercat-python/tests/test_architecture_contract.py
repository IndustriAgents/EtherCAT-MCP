"""Contract, authorization, audit and concurrency regressions (no device needed)."""

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from jsonschema import Draft202012Validator
from mcp.server.fastmcp import FastMCP

from ethercat_mcp.application.execution import Execution
from ethercat_mcp.domain.policy import Policy
from ethercat_mcp.protocol.contract import ContractBoundary, as_result, envelope_of, install_contract


def run(coro):
    return asyncio.run(coro)


def boundary(policy=None):
    calls = []

    async def call(name, arguments):
        calls.append((name, arguments))
        return as_result({"success": True, "data": arguments, "error": None, "meta": {}})

    b = ContractBoundary(SimpleNamespace(call_tool=call), policy or Policy())
    return b, calls


def sample(schema):
    if "const" in schema:
        return schema["const"]
    if "enum" in schema:
        return schema["enum"][0]
    if "anyOf" in schema:
        return sample(schema["anyOf"][0])
    kind = schema.get("type")
    if kind == "object":
        return {k: sample(schema["properties"][k]) for k in schema.get("required", [])}
    if kind == "array":
        return [sample(schema.get("items", {})) for _ in range(max(1, schema.get("minItems", 0)))]
    if kind in ("integer", "number"):
        return schema.get("minimum", 1)
    if kind == "boolean":
        return True
    if kind == "null":
        return None
    return "example"


def test_bundled_contract_matches_repository_and_is_valid():
    b, _ = boundary()
    root = Path(__file__).resolve().parents[2]
    assert b.contract == json.loads((root / "contract/tools.json").read_text())
    assert len(b.tools) == len(b.contract["tools"])
    for tool in b.tools.values():
        Draft202012Validator.check_schema(tool["inputSchema"])
        assert tool["description"]
    Draft202012Validator.check_schema(b.contract["resultSchema"])


def test_each_consolidated_mode_routes_once_with_selector_removed():
    b, calls = boundary()
    for name, route in b.contract["routes"].items():
        if "selector" not in route:
            continue
        for variant in b.tools[name]["inputSchema"]["oneOf"]:
            args = sample(variant)
            Draft202012Validator(b.tools[name]["inputSchema"]).validate(args)
            before = len(calls)
            value = envelope_of(run(b.call_tool(name, args)))
            assert value["success"], value
            assert len(calls) == before + 1
            selector = route["selector"]
            assert calls[-1] == (route["options"][args[selector]], {k: v for k, v in args.items() if k != selector})


def test_unknown_mode_and_extra_arguments_never_dispatch():
    b, calls = boundary()
    for name, route in b.contract["routes"].items():
        if "selector" not in route:
            continue
        bad = {route["selector"]: "not-a-mode"}
        assert envelope_of(run(b.call_tool(name, bad)))["success"] is False
        good = sample(b.tools[name]["inputSchema"]["oneOf"][0])
        good["misspelled_parameter"] = 1
        assert envelope_of(run(b.call_tool(name, good)))["success"] is False
    assert calls == []


def test_legacy_tools_cannot_bypass_read_only_or_allowlist():
    b, calls = boundary(Policy(read_only=True))
    for name, canonical in b.contract["legacy"].items():
        if b.tools[canonical]["annotations"].get("readOnlyHint"):
            continue
        args = sample(b.contract["legacySchemas"][name])
        result = envelope_of(run(b.call_tool(name, args)))
        assert result["success"] is False
        assert result["meta"]["outcome"] == "not_sent"
    assert calls == []
    b, calls = boundary(Policy(allowed_tools=()))
    for name, schema in b.contract["legacySchemas"].items():
        assert envelope_of(run(b.call_tool(name, sample(schema))))["success"] is False
    assert calls == []


def test_instances_do_not_share_admission_or_policy():
    a = Execution(Policy(max_in_flight=1, read_only=True))
    b = Execution(Policy(max_in_flight=1))
    first = a.admit("read", "read", write=False, local=False)
    with pytest.raises(ValueError):
        a.admit("read", "read", write=False, local=False)
    status = a.admit("status", "status", write=False, local=True)
    with pytest.raises(ValueError):
        a.admit("status", "status", write=False, local=True)
    control = b.admit("write", "write", write=True, local=False)
    with pytest.raises(ValueError):
        b.admit("write", "write", write=True, local=False)
    a.finish(first, None)
    a.finish(status, None)
    b.finish(control, None)
    assert (a.active, a.reserved, a.controls, b.active, b.controls) == (0, 0, 0, 0, 0)


def test_audit_intent_precedes_dispatch_and_never_logs_payload(tmp_path):
    path = tmp_path / "audit.jsonl"
    e = Execution(Policy(audit_path=str(path)))
    ticket = e.admit("write", "write", write=True, local=False)
    assert json.loads(path.read_text())["phase"] == "intent"
    e.finish(ticket, {"success": False, "data": "secret", "meta": {"outcome": "unknown", "request_sent": True}})
    entries = [json.loads(line) for line in path.read_text().splitlines()]
    assert entries[0]["call_id"] == entries[1]["call_id"]
    assert entries[1]["outcome"] == "unknown"
    assert "secret" not in path.read_text()
    e = Execution(Policy(audit_path=str(tmp_path / "missing" / "audit")))
    with pytest.raises(OSError):
        e.admit("write", "write", write=True, local=False)
    assert e.active == e.controls == 0


def test_wire_failure_sets_is_error_and_keeps_structured_envelope():
    b, _ = boundary(Policy(allowed_tools=()))
    name = next(iter(b.tools))
    args = sample(b.tools[name]["inputSchema"].get("oneOf", [b.tools[name]["inputSchema"]])[0])
    result = run(b.wire_call(name, args))
    assert result.isError is True
    assert result.structuredContent == json.loads(result.content[0].text)
    Draft202012Validator(b.contract["resultSchema"]).validate(result.structuredContent)


def test_sdk_callbacks_use_the_contract():
    server = FastMCP("contract-test")
    b = install_contract(server, Policy())
    assert [t.model_dump(exclude_none=True) for t in run(server.list_tools())] == b.contract["tools"]


@pytest.mark.parametrize(
    "value",
    [
        {"read_only": "true"},
        {"max_in_flight": True},
        {"max_in_flight": 0},
        {"allowed_tools": "write"},
        {"unknown": True},
        {"audit_path": ""},
    ],
)
def test_invalid_policy_fails_closed(value):
    with pytest.raises(ValueError):
        Policy.from_dict(value)


def test_only_declared_local_status_tools_use_reserved_capacity():
    b, calls = boundary(Policy(max_in_flight=1))
    ticket = b.execution.admit("busy", "busy", write=False, local=False)
    local_names = set(b.contract["localStatusTools"])
    assert local_names <= b.tools.keys()
    for name, tool in b.tools.items():
        args = sample(tool["inputSchema"].get("oneOf", [tool["inputSchema"]])[0])
        result = envelope_of(run(b.call_tool(name, args)))
        assert result["success"] is (name in local_names), name
    b.execution.finish(ticket, None)
