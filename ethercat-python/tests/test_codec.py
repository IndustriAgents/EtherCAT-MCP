from __future__ import annotations

import pytest

from ethercat_mcp.codec import decode_value, encode_value, parse_bytes, parse_int


@pytest.mark.parametrize(("raw", "expected"), [("0x1018", 0x1018), ("#x1A00", 0x1A00), ("4120", 4120), (4120, 4120)])
def test_parse_int_accepts_numbers_and_hex(raw: object, expected: int) -> None:
    assert parse_int(raw, "index") == expected


@pytest.mark.parametrize("raw", ["not-hex", "", "0xZZ", None, True, [1]])
def test_parse_int_rejects_garbage(raw: object) -> None:
    with pytest.raises(ValueError, match="index"):
        parse_int(raw, "index")


def test_parse_int_range() -> None:
    with pytest.raises(ValueError, match="minimum"):
        parse_int(-1, "slave_position", minimum=0)
    with pytest.raises(ValueError, match="maximum"):
        parse_int("0x10000", "index", maximum=0xFFFF)


def test_parse_bytes() -> None:
    assert parse_bytes([1, 0xFF, "0x10"]) == b"\x01\xff\x10"
    for bad in ([], [256], [-1], "abc", [True]):
        with pytest.raises(ValueError):
            parse_bytes(bad)


@pytest.mark.parametrize(
    ("value", "dtype", "raw"),
    [
        (1, "UINT8", b"\x01"),
        (-2, "INT16", b"\xfe\xff"),
        (0x12345678, "UDINT", b"\x78\x56\x34\x12"),
        (True, "BOOL", b"\x01"),
        (1.5, "REAL32", b"\x00\x00\xc0\x3f"),
        ("EL3102", "STRING", b"EL3102"),
        ("0a0b", "BYTES", b"\x0a\x0b"),
        ([1, 2], None, b"\x01\x02"),
    ],
)
def test_encode_round_trip(value: object, dtype: str | None, raw: bytes) -> None:
    payload, used = encode_value(value, dtype)
    assert payload == raw
    if dtype not in (None, "BYTES"):
        assert decode_value(payload, used)["value"] == value


def test_encode_errors() -> None:
    with pytest.raises(ValueError, match="data_type is required"):
        encode_value(5, None)
    with pytest.raises(ValueError, match="does not fit"):
        encode_value(300, "UINT8")
    with pytest.raises(ValueError, match="Unknown data_type"):
        encode_value(1, "WIDGET")
    with pytest.raises(ValueError, match="BOOL"):
        encode_value(2, "BOOL")


def test_decode_without_type_infers_unsigned() -> None:
    decoded = decode_value(b"\x02\x00\x00\x00", None)
    assert decoded == {"value": 2, "data_type": "UINT32", "data_type_inferred": True, "raw_hex": "02000000", "size": 4}
    assert decode_value(b"abc", None)["value"] is None


def test_decode_length_mismatch() -> None:
    with pytest.raises(ValueError, match="needs 4"):
        decode_value(b"\x01", "UINT32")
