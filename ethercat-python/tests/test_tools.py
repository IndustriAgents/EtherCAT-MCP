"""Tool layer: envelopes, gates, input validation, docstrings (known issues 6, 7, 11; review findings 2)."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from conftest import FIXTURES, envelope_from
from fake_pysoem import FakePysoem

from ethercat_mcp._version import __version__
from ethercat_mcp.config import EthercatConfig
from ethercat_mcp.ec_master import EthercatMaster, PysoemBackend
from ethercat_mcp.server import EthercatMCPServer

EXPECTED_TOOLS = {
    "get_master_status",
    "get_slave_info",
    "list_slaves",
    "load_esi_file",
    "ping",
    "read_pdo",
    "read_sdo",
    "read_slave_by_alias",
    "scan_network",
    "set_slave_state",
    "test_slave_communication",
    "write_pdo",
    "write_sdo",
    "write_slave_by_alias",
}


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
    assert server.mcp._mcp_server.version == __version__ == "0.1.0"


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
    ],
)
async def test_writes_and_state_changes_are_off_by_default(
    server: EthercatMCPServer, tool: str, arguments: dict[str, Any], message: str
) -> None:
    result = await call(server, tool, **arguments)
    assert result["success"] is False
    assert message in result["error"]
    assert result["meta"]["backend"] == "pysoem"


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
