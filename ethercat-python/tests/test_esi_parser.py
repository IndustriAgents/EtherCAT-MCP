"""ESI values live in child elements (known issue 5)."""

from __future__ import annotations

from pathlib import Path

import pytest
from conftest import FIXTURES

from ethercat_mcp.esi_parser import ESIParser, ESIParserError

SAMPLE = FIXTURES / "sample_esi.xml"


def test_vendor_and_device_identity_come_from_child_elements() -> None:
    doc = ESIParser().load(SAMPLE)
    assert doc["vendor"] == {"id": "0x00000002", "name": "Example Vendor"}
    el1008 = doc["devices"][0]
    assert el1008["type"] == "EL1008"
    assert el1008["name"] == "EL1008 8Ch. Dig. Input 24V, 3ms"  # LcId 1033 preferred
    assert el1008["product_code"] == "0x03F03052"
    assert el1008["revision"] == "0x00120000"
    assert el1008["group"] == "DigIn"


def test_object_dictionary_resolves_subitems_through_datatypes() -> None:
    el1008 = ESIParser().load(SAMPLE)["devices"][0]
    objects = {obj["index"]: obj for obj in el1008["object_dictionary"]}
    assert objects["0x1000"] == {
        "index": "0x1000",
        "name": "Device type",
        "type": "UDINT",
        "bit_size": 32,
        "access": "ro",
        "subindexes": [],
    }
    identity = objects["0x1018"]
    assert identity["name"] == "Identity"
    assert [(s["subindex"], s["name"], s["bit_size"]) for s in identity["subindexes"]] == [
        (0, "SubIndex 000", 8),
        (1, "Vendor ID", 32),
        (2, "Product code", 32),
    ]


def test_pdos() -> None:
    devices = ESIParser().load(SAMPLE)["devices"]
    tx = devices[0]["pdos"]["tx"]
    assert [p["index"] for p in tx] == ["0x1A00", "0x1A01"]
    assert tx[0]["entries"] == [
        {"index": "0x6000", "subindex": 1, "bit_length": 1, "name": "Input", "data_type": "BOOL"}
    ]
    assert tx[0]["sm"] == 0 and tx[0]["fixed"] is True
    assert devices[1]["pdos"]["rx"][0]["entries"][0]["index"] == "0x7000"


def test_select_summarises_several_devices_and_details_one() -> None:
    parser = ESIParser()
    many = parser.select(SAMPLE)
    assert many["matched"] == 2 and "hint" in many
    assert "object_dictionary" not in many["devices"][0]
    one = parser.select(SAMPLE, device_type="el2008", revision="0x00120000")
    assert one["matched"] == 1
    assert one["devices"][0]["pdos"]["rx"]
    with pytest.raises(ESIParserError, match="Available: EL1008 0x00120000, EL2008 0x00120000"):
        parser.select(SAMPLE, device_type="EL9999")


def test_esi_path_lookup_and_errors(tmp_path: Path) -> None:
    parser = ESIParser(FIXTURES)
    assert parser.resolve("sample_esi.xml") == FIXTURES / "sample_esi.xml"
    with pytest.raises(ESIParserError, match="not found"):
        parser.resolve("missing.xml")
    broken = tmp_path / "broken.xml"
    broken.write_text("<EtherCATInfo><Vendor>")
    with pytest.raises(ESIParserError, match="Failed to parse"):
        parser.load(broken)
    other = tmp_path / "other.xml"
    other.write_text("<Something/>")
    with pytest.raises(ESIParserError, match="not an ESI file"):
        parser.load(other)


def test_external_entities_are_not_resolved(tmp_path: Path) -> None:
    secret = tmp_path / "secret.txt"
    secret.write_text("TOP-SECRET")
    evil = tmp_path / "evil.xml"
    evil.write_text(
        f'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY s SYSTEM "file://{secret}">]>'
        "<EtherCATInfo><Vendor><Id>#x1</Id><Name>&s;</Name></Vendor></EtherCATInfo>"
    )
    doc = ESIParser().load(evil)
    assert "TOP-SECRET" not in str(doc)


def test_cache_follows_file_changes(tmp_path: Path) -> None:
    path = tmp_path / "esi.xml"
    path.write_text(SAMPLE.read_text())
    parser = ESIParser()
    assert len(parser.load(path)["devices"]) == 2
    path.write_text(SAMPLE.read_text().replace("EL2008", "EL2809") + "\n")
    assert parser.load(path)["devices"][1]["type"] == "EL2809"
