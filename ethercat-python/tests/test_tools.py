"""Tool layer: envelopes, gates, input validation, docstrings (known issues 6, 7, 11; review findings 2)."""

from __future__ import annotations

import json
from collections.abc import Iterator
from importlib.metadata import version
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from conftest import FIXTURES, envelope_from
from fake_pysoem import FakePysoem

from ethercat_mcp._version import __version__
from ethercat_mcp.config import EthercatConfig
from ethercat_mcp.ec_master import EthercatMaster, PysoemBackend
from ethercat_mcp.server import EthercatMCPServer

EXPECTED_TOOLS = {
    "ping",
    "scan_network",
    "read_pdo",
    "get_slave_info",
    "load_esi_file",
    "write_sdo",
    "read_sdo",
    "list_slaves",
    "test_slave_communication",
    "write_pdo",
    "set_slave_state",
}


@pytest.mark.anyio
@pytest.mark.parametrize("name", ["write_pdo", "write_sdo", "set_slave_state", "scan_network"])
async def test_unclassified_master_error_after_invocation_is_unknown(name):
    from ethercat_mcp.application.operations import Operations

    calls = []

    async def broken(*args):
        calls.append(args)
        raise ValueError("unexpected master result after dispatch")

    config = EthercatConfig.from_env({"ETHERCAT_WRITES_ENABLED": "true", "ETHERCAT_STATE_CHANGE_ENABLED": "true"})
    master = SimpleNamespace(
        describe=lambda: {"backend": "test"},
        write_pdo=broken,
        write_sdo=broken,
        set_slave_state=broken,
        scan_slaves=broken,
    )
    operations = Operations(SimpleNamespace(master=master, config=config), lambda: None)
    arguments = {
        "write_pdo": (0, 0, [1]),
        "write_sdo": (0, "0x2000", 0, 1, "UINT8"),
        "set_slave_state": (0, "OP"),
        "scan_network": (),
    }[name]
    result = await getattr(operations, name)(*arguments)
    assert result["success"] is False
    assert result["meta"]["outcome"] == "unknown"
    assert result["meta"]["request_sent"] is None
    assert len(calls) == 1


def build(tmp_path: Path, slave_map: Any = None, **env: str) -> tuple[EthercatMCPServer, FakePysoem]:
    settings = {"ETHERCAT_INTERFACE": "fake0", "ETHERCAT_TIMEOUT": "100000", **env}
    if slave_map is not None:
        path = tmp_path / "slaves.json"
        path.write_text(slave_map if isinstance(slave_map, str) else json.dumps(slave_map))
        settings["SLAVE_MAP_FILE"] = str(path)
    config = EthercatConfig.from_env(settings)
    fake = FakePysoem()
    master = EthercatMaster(config, backend=PysoemBackend(config, pysoem_module=fake))
    return EthercatMCPServer(config, master=master), fake


async def call(server: EthercatMCPServer, name: str, **arguments: Any) -> dict[str, Any]:
    return envelope_from(await server.mcp.call_tool(name, arguments))


@pytest.mark.anyio
async def test_invalid_sdo_type_is_rejected_before_native_io(tmp_path):
    server, fake = build(tmp_path)
    called = []

    async def read_sdo(*args):
        called.append(args)
        return b"\x00", {}

    server.master.read_sdo = read_sdo
    result = await call(server, "read_sdo", slave_position=0, index="0x1000", data_type="WIDGET")
    assert not result["success"] and "data_type" in result["error"]
    assert called == []


@pytest.mark.anyio
async def test_unsafe_64_bit_sdo_value_is_lossless_on_mcp_wire(tmp_path):
    from mcp.shared.memory import create_connected_server_and_client_session

    server, _ = build(tmp_path)

    async def read_sdo(*args):
        return bytes.fromhex("ffffffffffffffff"), {}

    server.master.read_sdo = read_sdo
    async with create_connected_server_and_client_session(server.mcp) as session:
        result = await session.call_tool("read_sdo", {"slave_position": 0, "index": "0x2000", "data_type": "UINT64"})
    envelope = result.structuredContent
    assert json.loads(result.content[0].text) == envelope
    assert envelope["data"]["value"] == "18446744073709551615"
    assert envelope["data"]["value_encoding"] == "decimal_string"


@pytest.mark.anyio
async def test_capability_status_is_local_and_distinguishes_gates(tmp_path):
    from jsonschema import Draft202012Validator

    server, fake = build(tmp_path)
    result = await call(server, "ping")
    caps = result["data"]["capabilities"]
    assert caps["scope"] == "backend_support" and caps["device_verified"] is False
    assert caps["operations"]["read_sdo"]["supported"] is True
    assert caps["operations"]["write_sdo"]["enabled"] is False
    assert caps["operations"]["set_slave_state"]["enabled"] is False
    assert server.master.backend._master is None
    advertised = {t.name: t for t in await server.mcp.list_tools()}
    Draft202012Validator(advertised["ping"].outputSchema).validate(result)


@pytest.fixture
def server(tmp_path: Path) -> Iterator[EthercatMCPServer]:
    srv, _ = build(tmp_path)
    yield srv
    srv.master.backend.close()


@pytest.mark.anyio
async def test_every_tool_is_listed_with_a_description(server: EthercatMCPServer) -> None:
    tools = await server.mcp.list_tools()
    assert {t.name for t in tools} == EXPECTED_TOOLS
    for tool in tools:
        assert tool.description and len(tool.description) > 30, tool.name
    by_name = {t.name: t for t in tools}
    assert by_name["read_sdo"].annotations.readOnlyHint is True
    assert by_name["write_pdo"].annotations.destructiveHint is True


def test_server_version_is_the_package_version(server: EthercatMCPServer) -> None:
    assert server.mcp._mcp_server.version == __version__ == version("industriagents-ethercat-mcp")


@pytest.mark.anyio
async def test_ping_reports_backend_and_read_only_defaults(tmp_path: Path) -> None:
    srv, fake = build(tmp_path)
    result = await call(srv, "ping")
    assert result["success"] is True
    assert result["meta"]["backend"] == "pysoem"
    assert result["data"]["writes_enabled"] is False
    assert result["data"]["state_change_enabled"] is False
    assert result["data"]["connection"]["writes_enabled"] is False
    assert fake.masters == []  # no bus access


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("tool", "arguments", "message"),
    [
        ("write_pdo", {"slave_position": 0, "offset": 0, "data": [1]}, "ETHERCAT_WRITES_ENABLED"),
        ("write_sdo", {"slave_position": 0, "index": "0x8000", "subindex": 6, "value": 1}, "ETHERCAT_WRITES_ENABLED"),
        ("write_slave_by_alias", {"alias": "x", "data": [1]}, "ETHERCAT_WRITES_ENABLED"),
        ("set_slave_state", {"slave_position": 0, "state": "OP"}, "ETHERCAT_STATE_CHANGE_ENABLED"),
        ("set_slave_state", {"slave_position": 0, "state": "OP"}, "ETHERCAT_WRITES_ENABLED"),
    ],
)
async def test_writes_and_state_changes_are_off_by_default(
    server: EthercatMCPServer, tool: str, arguments: dict[str, Any], message: str
) -> None:
    result = await call(server, tool, **arguments)
    assert result["success"] is False
    assert message in result["error"]
    assert result["meta"]["backend"] == "pysoem"
    assert result["meta"]["outcome"] == "not_sent" and result["meta"]["request_sent"] is False


@pytest.mark.anyio
@pytest.mark.parametrize("gate", ["ETHERCAT_STATE_CHANGE_ENABLED", "ETHERCAT_WRITES_ENABLED"])
async def test_state_changes_need_both_gates(tmp_path: Path, gate: str) -> None:
    """Suite rule 2: a separate state gate never replaces the write gate."""
    srv, fake = build(tmp_path, **{gate: "true"})
    try:
        result = await call(srv, "set_slave_state", slave_position=0, state="PREOP")
        assert result["success"] is False
        assert "need both ETHERCAT_WRITES_ENABLED=true and ETHERCAT_STATE_CHANGE_ENABLED=true" in result["error"]
        assert fake.masters == []
    finally:
        srv.master.backend.close()


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("tool", "arguments", "field"),
    [
        ("get_slave_info", {"slave_position": True}, "slave_position"),
        ("read_pdo", {"slave_position": 3, "offset": False}, "offset"),
        ("read_sdo", {"slave_position": 3, "index": True}, "index"),
        ("read_sdo", {"slave_position": 3, "index": 4120.0}, "index"),
        ("write_pdo", {"slave_position": 2, "offset": 0, "data": [True]}, "data.0"),
        ("write_sdo", {"slave_position": 3, "index": "0x8000", "subindex": True, "value": 1}, "subindex"),
    ],
)
async def test_booleans_and_floats_are_not_integers(
    tmp_path: Path, tool: str, arguments: dict[str, Any], field: str
) -> None:
    """Suite rule 7: strict integers for positions, indexes and byte values."""
    srv, fake = build(tmp_path, ETHERCAT_WRITES_ENABLED="true")
    try:
        result = await call(srv, tool, **arguments)
        assert result["success"] is False, result
        assert result["error"].startswith(f"Invalid arguments for {tool}: {field}"), result["error"]
        assert fake.masters == []  # rejected before anything touched the bus
    finally:
        srv.master.backend.close()


@pytest.mark.anyio
async def test_write_outcomes_are_reported(tmp_path: Path) -> None:
    srv, fake = build(tmp_path, ETHERCAT_WRITES_ENABLED="true", ETHERCAT_STATE_CHANGE_ENABLED="true")
    fake.bus.devices[3].spec.sdo_reply_lost = True
    try:
        await call(srv, "scan_network")
        lost = await call(srv, "write_sdo", slave_position=3, index="0x8000", subindex=6, value=1, data_type="UINT8")
        assert lost["success"] is False
        assert lost["meta"]["outcome"] == "unknown" and lost["meta"]["request_sent"] is True
        assert "may have been applied; check/read back before retrying" in lost["error"]
        aborted = await call(srv, "write_sdo", slave_position=3, index="0x2000", subindex=0, value=1, data_type="UINT8")
        assert aborted["meta"]["outcome"] == "rejected"
        refused = await call(srv, "set_slave_state", slave_position=1, state="OP")
        assert refused["meta"]["outcome"] == "rejected"
        moved = await call(srv, "set_slave_state", slave_position=2, state="SAFEOP")
        assert moved["success"] and moved["meta"]["outcome"] == "written" and moved["meta"]["request_sent"]
        too_big = await call(srv, "write_pdo", slave_position=2, offset=0, data=[0] * 4097)
        assert "at most 4096" in too_big["error"] and too_big["meta"]["outcome"] == "not_sent"
    finally:
        srv.master.backend.close()


@pytest.mark.anyio
async def test_bad_sdo_index_is_an_envelope(server: EthercatMCPServer) -> None:
    await call(server, "scan_network")
    result = await call(server, "read_sdo", slave_position=3, index="not-hex", subindex=0)
    assert result["success"] is False
    assert "index 'not-hex' is not a number" in result["error"]
    result = await call(server, "read_sdo", slave_position=3, index="0x10000")
    assert result["success"] is False and "maximum" in result["error"]


@pytest.mark.anyio
async def test_negative_position_is_rejected(server: EthercatMCPServer) -> None:
    await call(server, "scan_network")
    for tool in ("get_slave_info", "read_pdo", "test_slave_communication"):
        result = await call(server, tool, slave_position=-1)
        assert result["success"] is False, tool
        assert "slave_position -1 is below the minimum 0" in result["error"]


@pytest.mark.anyio
async def test_wrong_argument_types_come_back_as_envelopes(server: EthercatMCPServer) -> None:
    result = await call(server, "get_slave_info", slave_position="abc")
    assert result["success"] is False
    assert result["error"].startswith("Invalid arguments for get_slave_info: slave_position")
    assert result["meta"]["backend"] == "pysoem"
    result = await call(server, "read_pdo")
    assert result["success"] is False and "slave_position" in result["error"]


@pytest.mark.anyio
async def test_read_flow_against_fake_bus(tmp_path: Path) -> None:
    srv, _ = build(tmp_path, ETHERCAT_STATE_CHANGE_ENABLED="true", ETHERCAT_WRITES_ENABLED="true")
    try:
        assert (await call(srv, "get_slave_info", slave_position=0))["error"].startswith("No bus scan yet")
        scan = await call(srv, "scan_network")
        assert scan["success"] and len(scan["data"]["slaves"]) == 4
        info = await call(srv, "get_slave_info", slave_position=3)
        assert info["data"]["name"] == "EL3102"
        assert (await call(srv, "get_slave_info", slave_position=9))["success"] is False

        sdo = await call(srv, "read_sdo", slave_position=3, index="0x1018", subindex=1, data_type="UDINT")
        assert sdo["data"]["value"] == 2 and sdo["data"]["data_type"] == "UINT32"
        wrote = await call(srv, "write_sdo", slave_position=3, index="0x8000", subindex=6, value=True, data_type="BOOL")
        assert wrote["success"] and wrote["data"]["raw_hex"] == "01"
        bad = await call(srv, "write_sdo", slave_position=3, index="0x8000", subindex=6, value=1)
        assert bad["success"] is False and "data_type is required" in bad["error"]

        for state in ("SAFEOP", "OP"):
            assert (await call(srv, "set_slave_state", slave_position=2, state=state))["success"]
        assert (await call(srv, "set_slave_state", slave_position=1, state="safe-op"))["success"]
        assert (await call(srv, "set_slave_state", slave_position=1, state="BOOT"))["success"] is False
        bad = await call(srv, "write_pdo", slave_position=2, offset=0, data=[256])
        assert bad["success"] is False and "not a byte value" in bad["error"]
        wrote = await call(srv, "write_pdo", slave_position=2, offset=0, data=[0x5A])
        assert wrote["success"] and wrote["meta"]["wkc"] > 0
        read = await call(srv, "read_pdo", slave_position=1, offset=0, length=1)
        assert read["data"]["raw_data_hex"] == "5a"
        status = await call(srv, "get_master_status")
        assert status["data"]["process_image_mapped"] is True
    finally:
        await srv.master.close()


@pytest.mark.anyio
async def test_alias_specs_are_validated(tmp_path: Path) -> None:
    srv, _ = build(
        tmp_path,
        slave_map={
            "good": {"position": 1, "offset": 0, "length": 1, "description": "inputs"},
            "letters": {"position": "abc"},
            "negative": {"position": -1},
            "no_position": {"offset": 0},
            "not_object": 5,
        },
    )
    listing = await call(srv, "list_slaves")
    assert listing["success"] and listing["data"]["count"] == 5
    errors = {entry["alias"]: entry.get("error") for entry in listing["data"]["slaves"]}
    assert errors["good"] is None
    assert "position of alias 'letters' 'abc' is not a number" in errors["letters"]
    for alias in ("letters", "negative", "no_position", "not_object", "unknown"):
        result = await call(srv, "read_slave_by_alias", alias=alias)
        assert result["success"] is False, alias
        assert result["meta"]["alias"] == alias
    result = await call(srv, "read_slave_by_alias", alias="good")
    assert result["success"] is False and "No bus scan yet" in result["error"]
    assert result["meta"]["alias"] == "good"


@pytest.mark.anyio
async def test_broken_slave_map_is_reported(tmp_path: Path) -> None:
    srv, _ = build(tmp_path, slave_map="{not json")
    result = await call(srv, "list_slaves")
    assert result["success"] is False and "not valid JSON" in result["error"]
    assert "not valid JSON" in (await call(srv, "ping"))["data"]["slave_map_error"]


@pytest.mark.anyio
async def test_load_esi_file_tool(server: EthercatMCPServer) -> None:
    result = await call(server, "load_esi_file", filepath=str(FIXTURES / "sample_esi.xml"), device_type="EL1008")
    assert result["success"] is True
    device = result["data"]["devices"][0]
    assert device["product_code"] == "0x03F03052"
    assert device["object_dictionary"][0]["index"] == "0x1000"
    missing = await call(server, "load_esi_file", filepath="/nonexistent/esi.xml")
    assert missing["success"] is False and "not found" in missing["error"]


@pytest.mark.anyio
async def test_annotations_never_invite_retries_of_device_changes(server: EthercatMCPServer) -> None:
    """Suite rule 13."""
    changing = {"scan_network", "write_pdo", "write_sdo", "set_slave_state", "write_slave_by_alias"}
    for tool in await server.mcp.list_tools():
        hints = tool.annotations
        if tool.name in changing:
            assert hints.readOnlyHint is False, tool.name
            assert hints.destructiveHint is True and hints.idempotentHint is False, tool.name
        else:
            assert hints.readOnlyHint is True, tool.name


@pytest.mark.anyio
async def test_invalid_arguments_to_a_write_tool_are_not_sent(server: EthercatMCPServer) -> None:
    result = await call(server, "write_pdo", slave_position="2", offset=0, data=[1])
    assert result["success"] is False and result["meta"]["outcome"] == "not_sent"
    assert result["meta"]["request_sent"] is False
    read = await call(server, "read_pdo", slave_position="2")
    assert "outcome" not in read["meta"]


@pytest.mark.anyio
async def test_scan_reports_its_outcome_and_what_it_reset(tmp_path: Path) -> None:
    srv, _ = build(tmp_path)
    try:
        result = await call(srv, "scan_network")
        meta = result["meta"]
        assert meta["outcome"] == "written" and meta["request_sent"] is True
        assert meta["segment_state_before"] == "INIT" and meta["reset_live_segment"] is False
    finally:
        srv.master.backend.close()


@pytest.mark.anyio
async def test_every_device_call_has_an_overall_deadline(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Suite rule 12: a hung device call ends. A write that went out is unknown, one that never
    got to send is not_sent, and a read is a clean error."""
    import time as time_module

    from ethercat_mcp.config import EthercatConfig

    monkeypatch.setattr(EthercatConfig, "call_deadline_s", property(lambda self: 0.3))
    srv, _ = build(tmp_path, ETHERCAT_WRITES_ENABLED="true")
    backend = srv.master.backend
    try:
        await call(srv, "scan_network")

        def hung_send(*args, token):
            token.begin_send()
            time_module.sleep(3)

        def hung_before_send(*args, token):
            time_module.sleep(3)

        start = time_module.monotonic()
        monkeypatch.setattr(backend, "write_sdo", hung_send)
        wrote = await call(srv, "write_sdo", slave_position=3, index="0x8000", subindex=6, value=1, data_type="UINT8")
        assert wrote["success"] is False and wrote["meta"]["outcome"] == "unknown"
        assert wrote["meta"]["request_sent"] is True and "did not finish within" in wrote["error"]
        monkeypatch.setattr(backend, "write_sdo", hung_before_send)
        queued = await call(srv, "write_sdo", slave_position=3, index="0x8000", subindex=6, value=1, data_type="UINT8")
        assert queued["meta"]["outcome"] == "not_sent" and queued["meta"]["request_sent"] is False
        assert "will not be sent later" in queued["error"]
        monkeypatch.setattr(backend, "read_sdo", hung_before_send)
        read = await call(srv, "read_sdo", slave_position=3, index="0x1000")
        assert read["success"] is False and "did not finish within" in read["error"]
        assert time_module.monotonic() - start < 3.0
    finally:
        backend.close()


@pytest.mark.anyio
async def test_esi_parsing_runs_off_the_event_loop_with_a_deadline(
    server: EthercatMCPServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    import time as time_module

    from ethercat_mcp.application import metadata as tools

    monkeypatch.setattr(tools, "ESI_DEADLINE_S", 0.3)
    monkeypatch.setattr(server.esi_parser, "select", lambda *args: time_module.sleep(3))
    start = time_module.monotonic()
    result = await call(server, "load_esi_file", filepath="slow.xml")
    assert result["success"] is False and "did not finish within" in result["error"]
    assert time_module.monotonic() - start < 2.0


@pytest.mark.anyio
@pytest.mark.parametrize("max_bytes,data_type", [(0, "BYTES"), (4097, "STRING"), (3, "UINT32"), (7, "UINT64")])
async def test_upload_buffer_and_fixed_type_validated_before_access(tmp_path, max_bytes, data_type):
    srv, _ = build(tmp_path)

    async def never(*_args):
        pytest.fail("Invalid upload sizing must not access a slave")

    srv.master.read_sdo = never
    result = await call(srv, "read_sdo", slave_position=0, index="0x2000", data_type=data_type, max_bytes=max_bytes)
    assert result["success"] is False and "max_bytes" in result["error"]


@pytest.mark.anyio
async def test_large_sdo_mcp_wire_uses_requested_buffer(tmp_path):
    from mcp.shared.memory import create_connected_server_and_client_session

    srv, fake = build(tmp_path)
    await srv.master.scan_slaves()
    value = bytes(range(256)) * 2
    slave = srv.master.backend._slaves[3]
    slave._dev.objects[(0x8000, 6)][0] = value
    async with create_connected_server_and_client_session(srv.mcp) as session:
        result = await session.call_tool(
            "read_sdo",
            {
                "slave_position": 3,
                "index": "0x8000",
                "subindex": 6,
                "data_type": "BYTES",
                "max_bytes": 512,
            },
        )
    envelope = result.structuredContent
    assert envelope["success"] is True
    assert envelope["data"]["value"] == list(value)
    assert envelope["data"]["raw_hex"] == value.hex()
    assert envelope["meta"]["max_bytes"] == 512 and envelope["meta"]["complete"] is True
    assert slave.last_upload_size == 512
    srv.master.backend.close()
