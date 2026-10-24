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


@pytest.mark.parametrize("validation_failure", [False, True])
def test_unexpected_control_failure_preserves_uncertainty_and_audit(tmp_path, validation_failure):
    from mcp.types import ToolAnnotations

    from ethercat_mcp.server import EnvelopeFastMCP

    sent = []
    server = EnvelopeFastMCP("unexpected-test", envelope_meta=lambda: {})

    @server.tool(name="write_slave_by_alias", annotations=ToolAnnotations(readOnlyHint=False))
    async def broken_control(alias: str, data: list[int]) -> dict:
        sent.append((alias, data))
        if validation_failure:
            from pydantic import BaseModel

            class Reply(BaseModel):
                count: int

            Reply.model_validate({"count": "private payload must not escape the boundary"})
        raise RuntimeError("private payload must not escape the boundary")

    audit_path = tmp_path / "audit.jsonl"
    b = install_contract(server, Policy(audit_path=str(audit_path)))
    result = run(b.wire_call("write_slave_by_alias", {"alias": "output", "data": [1]}))
    assert sent == [("output", [1])]
    assert result.isError is True
    value = result.structuredContent
    assert value["success"] is False
    assert value["meta"]["outcome"] == "unknown"
    assert value["meta"]["request_sent"] is None
    assert value["meta"]["code"] == "internal_error"
    assert "private payload" not in result.content[0].text
    assert b.execution.active == b.execution.controls == 0
    entries = [json.loads(line) for line in audit_path.read_text().splitlines()]
    assert [entry["phase"] for entry in entries] == ["intent", "result"]
    assert entries[-1]["outcome"] == "unknown"
    assert entries[-1]["request_sent"] is None


def test_cancellation_releases_admission_and_records_unknown_outcome(tmp_path):
    b, _ = boundary(Policy(audit_path=str(tmp_path / "audit.jsonl")))

    async def cancelled(name, arguments):
        raise asyncio.CancelledError()

    b.original_call = cancelled
    name = next(name for name, tool in b.tools.items() if not tool["annotations"]["readOnlyHint"])
    args = sample(b.tools[name]["inputSchema"].get("oneOf", [b.tools[name]["inputSchema"]])[0])
    with pytest.raises(asyncio.CancelledError):
        run(b.call_tool(name, args))
    assert b.execution.active == b.execution.controls == 0
    entries = [json.loads(line) for line in (tmp_path / "audit.jsonl").read_text().splitlines()]
    assert entries[-1]["outcome"] == "unknown"


def test_unknown_arguments_rejected_on_direct_routes_and_legacy_aliases():
    b, calls = boundary()
    for name, schema in b.contract["legacySchemas"].items():
        args = sample(schema)
        args["misspelled_parameter"] = "do not dispatch"
        value = envelope_of(run(b.call_tool(name, args)))
        assert value["success"] is False, name
        assert value["meta"]["code"] == "invalid_arguments", name
    assert calls == []


def test_bounded_requests_refuse_before_dispatch_or_audit(monkeypatch):
    b, calls = boundary()
    name = next(n for n, t in b.tools.items() if not t["annotations"].get("readOnlyHint"))
    schema = b.tools[name]["inputSchema"]
    base = sample(schema.get("oneOf", [schema])[0])
    audits = []
    monkeypatch.setattr(b.execution.audit, "record", lambda *args: audits.append(args))
    deep = []
    for _ in range(b.contract["limits"]["maxNestingDepth"] + 1):
        deep = [deep]
    cycle = []
    cycle.append(cycle)
    oversized = [None] * (b.contract["limits"]["maxContainerItems"] + 1)
    broad = [[None] * 10000 for _ in range(6)]
    for payload in [deep, cycle, oversized, broad, "x" * (b.execution.policy.max_request_bytes + 1)]:
        value = envelope_of(run(b.call_tool(name, {**base, "oversized": payload})))
        assert value["success"] is False
        assert value["meta"]["code"] == "resource_limit"
        assert (value["meta"]["outcome"], value["meta"]["request_sent"]) == ("not_sent", False)
    assert calls == audits == []
    assert b.execution.active == b.execution.controls == 0


@pytest.mark.parametrize("payload", [float("nan"), float("inf"), object(), {1: "invalid key"}, "\ud800"])
def test_non_json_arguments_fail_as_envelopes(payload):
    b, calls = boundary()
    name = next(iter(b.tools))
    value = envelope_of(run(b.call_tool(name, {"invalid": payload})))
    assert value["success"] is False
    assert value["meta"]["code"] == "invalid_arguments"
    assert calls == []


def test_request_limits_include_exact_escaped_encoding_and_edges():
    from ethercat_mcp.protocol.request_limits import RequestError, validate_request

    limits = {"maxNestingDepth": 2, "maxContainerItems": 2, "maxTotalValues": 4}
    validate_request({"a": [1, 2]}, 1024, limits)
    with pytest.raises(RequestError, match="maxNestingDepth"):
        validate_request({"a": [[1]]}, 1024, limits)
    with pytest.raises(RequestError, match="maxContainerItems"):
        validate_request({"a": [1, 2, 3]}, 1024, limits)
    with pytest.raises(RequestError, match="max_request_bytes"):
        validate_request({"a": "\u0000" * 200}, 1024, limits)


@pytest.mark.parametrize("text_only", [False, True])
def test_audit_result_failure_preserves_outcome_and_text_parity(monkeypatch, text_only):
    b, calls = boundary()
    name = next(n for n, t in b.tools.items() if not t["annotations"].get("readOnlyHint"))
    schema = b.tools[name]["inputSchema"]
    args = sample(schema.get("oneOf", [schema])[0])

    def record(ticket, phase, result):
        if phase == "result":
            raise OSError("audit unavailable after send")

    async def written(*args):
        result = as_result(
            {"success": True, "data": None, "error": None, "meta": {"outcome": "written", "request_sent": True}}
        )
        return result[0] if text_only else result

    b.original_call = written
    monkeypatch.setattr(b.execution.audit, "record", record)
    result = run(b.call_tool(name, args))
    value = envelope_of(result)
    assert json.loads(result[0][0].text) == value
    assert value["success"] is True
    assert value["meta"] == {"outcome": "written", "request_sent": True, "audit_error": True}
    assert b.execution.active == b.execution.controls == 0


def test_application_and_domain_do_not_import_transport_or_native_drivers():
    import ast

    root = Path(__file__).resolve().parents[1] / "src/ethercat_mcp"
    forbidden = {"mcp", "pysoem", "serial", "scapy"}
    for layer in ["application", "domain"]:
        for path in (root / layer).glob("*.py"):
            for node in ast.walk(ast.parse(path.read_text())):
                if isinstance(node, ast.Import):
                    imports = [entry.name for entry in node.names]
                elif isinstance(node, ast.ImportFrom):
                    imports = [node.module or ""]
                else:
                    continue
                assert not any(name.split(".")[0] in forbidden for name in imports), path
                assert not any("adapters" in name.split(".") or "backends" in name.split(".") for name in imports), path


def test_request_bounds_precede_selector_argument_copy():
    class UncopyableArguments(dict):
        def items(self):
            raise AssertionError("Unbounded request was copied before validation")

    b, calls = boundary()
    name, route = next((name, route) for name, route in b.contract["routes"].items() if "selector" in route)
    args = UncopyableArguments({str(n): None for n in range(b.contract["limits"]["maxContainerItems"])})
    args[route["selector"]] = next(iter(route["options"]))
    result = envelope_of(run(b.call_tool(name, args)))
    assert result["success"] is False
    assert result["meta"]["code"] == "resource_limit"
    assert calls == []
