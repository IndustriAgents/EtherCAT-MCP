"""ESI values live in child elements (known issue 5)."""

from __future__ import annotations

import time
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


def test_external_entities_are_refused(tmp_path: Path) -> None:
    secret = tmp_path / "secret.txt"
    secret.write_text("TOP-SECRET")
    evil = tmp_path / "evil.xml"
    evil.write_text(
        f'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY s SYSTEM "file://{secret}">]>'
        "<EtherCATInfo><Vendor><Id>#x1</Id><Name>&s;</Name></Vendor></EtherCATInfo>"
    )
    with pytest.raises(ESIParserError, match="declares a DOCTYPE or ENTITY"):
        ESIParser().load(evil)


def write_attribute_bomb(path: Path) -> None:
    """The reviewer's case: a 100 KB internal entity used 2000 times in an attribute value.

    libxml2 expands entities in attribute values even with resolve_entities=False; at
    the lxml 4.9.3 floor this took 6 s and 400 MB and parsed "successfully".
    """
    ent = "A" * 100_000
    refs = "&a;" * 2000
    path.write_text(
        f'<?xml version="1.0"?>\n<!DOCTYPE EtherCATInfo [<!ENTITY a "{ent}">]>\n'
        f"<EtherCATInfo><Vendor><Id>#x00000002</Id></Vendor><Descriptions><Devices><Device>"
        f'<Type ProductCode="{refs}" RevisionNo="#x00120000">EL1008</Type></Device></Devices>'
        f"</Descriptions></EtherCATInfo>\n"
    )


def test_attribute_entity_bomb_is_refused_before_parsing(tmp_path: Path) -> None:
    bomb = tmp_path / "bomb.xml"
    write_attribute_bomb(bomb)
    start = time.perf_counter()
    with pytest.raises(ESIParserError, match="declares a DOCTYPE or ENTITY"):
        ESIParser().select(bomb)
    assert time.perf_counter() - start < 1.0


@pytest.mark.parametrize("codec", ["utf-16", "utf-16-le", "utf-32"])
def test_declarations_are_found_in_other_encodings(tmp_path: Path, codec: str) -> None:
    doc = tmp_path / "wide.xml"
    text = '<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "b">]><EtherCATInfo/>'
    doc.write_bytes(text.encode(codec))
    with pytest.raises(ESIParserError, match="declares a DOCTYPE or ENTITY"):
        ESIParser().load(doc)


def bomb_text(encoding: str) -> str:
    ent = "A" * 100_000
    refs = "&a;" * 2000
    return (
        f'<?xml version="1.0" encoding="{encoding}"?>\n<!DOCTYPE EtherCATInfo [<!ENTITY a "{ent}">]>\n'
        f"<EtherCATInfo><Vendor><Id>#x00000002</Id></Vendor><Descriptions><Devices><Device>"
        f'<Type ProductCode="{refs}" RevisionNo="#x00120000">EL1008</Type></Device></Devices>'
        f"</Descriptions></EtherCATInfo>\n"
    )


@pytest.mark.parametrize(
    ("name", "content"),
    [
        # The reviewer's bypass: at lxml 4.9.3 this took 7 s and 400 MB and parsed "successfully".
        ("utf7", lambda: bomb_text("UTF-7").encode("utf-7")),
        ("utf7-ascii-markup", lambda: bomb_text("UTF-7").replace("<!", "+ADwAIQ-").encode("ascii")),
        ("ebcdic", lambda: bomb_text("IBM037").encode("cp037")),
        ("windows-1252", lambda: bomb_text("windows-1252").encode("cp1252")),
        # The PROFINET reviewer's bypass: the encoding pushed past the first kilobyte of the declaration.
        (
            "utf7-padded-declaration",
            lambda: (
                ('<?xml version="1.0"' + " " * 1100 + 'encoding="UTF-7"?>').encode("ascii")
                + bomb_text("UTF-8").split("?>", 1)[1].encode("utf-7").replace(b"<!", b"+ADwAIQ-")
            ),
        ),
        (
            "ebcdic-padded-declaration",
            lambda: bomb_text("IBM037").replace('version="1.0"', 'version="1.0"' + " " * 1100).encode("cp037"),
        ),
        ("ebcdic-no-declaration", lambda: bomb_text("IBM037").split("?>", 1)[1].encode("cp037")),
        ("ibm500", lambda: bomb_text("IBM500").encode("cp500")),
    ],
)
def test_encodings_outside_the_whitelist_are_refused_before_parsing(tmp_path: Path, name: str, content) -> None:
    doc = tmp_path / f"{name}.xml"
    doc.write_bytes(content())
    start = time.perf_counter()
    with pytest.raises(ESIParserError, match="Refusing"):
        ESIParser().select(doc)
    assert time.perf_counter() - start < 1.0


def test_unterminated_declaration_is_refused(tmp_path: Path) -> None:
    doc = tmp_path / "open.xml"
    doc.write_bytes(b'<?xml version="1.0" encoding="UTF-8" <EtherCATInfo/>')
    with pytest.raises(ESIParserError, match="not terminated"):
        ESIParser().load(doc)


def test_utf16_with_bom_is_decoded_in_python(tmp_path: Path) -> None:
    text = SAMPLE.read_text(encoding="utf-8").replace('encoding="UTF-8"', 'encoding="UTF-16"')
    doc = tmp_path / "wide.xml"
    doc.write_bytes(text.encode("utf-16"))  # with BOM
    assert ESIParser().load(doc)["devices"][0]["type"] == "EL1008"
    bomb = tmp_path / "wide-bomb.xml"
    bomb.write_bytes(bomb_text("UTF-16").encode("utf-16"))
    with pytest.raises(ESIParserError, match="DOCTYPE or ENTITY"):
        ESIParser().load(bomb)


def test_latin1_text_is_decoded_as_declared(tmp_path: Path) -> None:
    text = SAMPLE.read_text(encoding="utf-8").replace('encoding="UTF-8"', 'encoding="ISO8859-1"')
    doc = tmp_path / "latin1.xml"
    doc.write_bytes(text.replace("Example Vendor", "Müller GmbH").encode("latin-1"))
    assert ESIParser().load(doc)["vendor"]["name"] == "Müller GmbH"


def test_declared_encoding_must_match_the_byte_layout(tmp_path: Path) -> None:
    doc = tmp_path / "mismatch.xml"
    doc.write_bytes(b'<?xml version="1.0" encoding="UTF-16"?><EtherCATInfo/>')
    with pytest.raises(ESIParserError, match="does not match its bytes"):
        ESIParser().load(doc)


@pytest.mark.parametrize("encoding", ["ISO8859-1", "ISO-8859-1", "UTF-8", "us-ascii"])
def test_whitelisted_encodings_parse(tmp_path: Path, encoding: str) -> None:
    text = SAMPLE.read_text(encoding="utf-8").replace('encoding="UTF-8"', f'encoding="{encoding}"')
    doc = tmp_path / "ok.xml"
    doc.write_bytes(text.encode("latin-1" if "8859" in encoding else "utf-8"))
    assert ESIParser().load(doc)["vendor"]["name"] == "Example Vendor"


def test_billion_laughs_in_text_is_refused(tmp_path: Path) -> None:
    lol = ['<!ENTITY l0 "lol">'] + [f'<!ENTITY l{i} "{f"&l{i - 1};" * 10}">' for i in range(1, 10)]
    doc = tmp_path / "laughs.xml"
    doc.write_text(
        f'<?xml version="1.0"?><!DOCTYPE x [{"".join(lol)}]><EtherCATInfo><Vendor>&l9;</Vendor></EtherCATInfo>'
    )
    with pytest.raises(ESIParserError, match="DOCTYPE or ENTITY"):
        ESIParser().load(doc)


def test_size_cap(tmp_path: Path) -> None:
    big = tmp_path / "big.xml"
    big.write_bytes(SAMPLE.read_bytes() + b" " * 2048)
    with pytest.raises(ESIParserError, match="the limit is 1024"):
        ESIParser(max_bytes=1024).load(big)
    assert ESIParser().load(big)["devices"]


def test_cache_follows_file_changes(tmp_path: Path) -> None:
    path = tmp_path / "esi.xml"
    path.write_text(SAMPLE.read_text())
    parser = ESIParser()
    assert len(parser.load(path)["devices"]) == 2
    path.write_text(SAMPLE.read_text().replace("EL2008", "EL2809") + "\n")
    assert parser.load(path)["devices"][1]["type"] == "EL2809"
