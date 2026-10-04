"""Argument parsing and CoE (SDO) value encoding.

CoE values travel as little-endian byte strings. PySOEM's ``sdo_read`` returns
``bytes`` and ``sdo_write`` takes ``bytes``, so the server needs to know the
data type to turn a value into bytes and back. These helpers raise
``ValueError`` with a message that can go straight into an error envelope.
"""

from __future__ import annotations

import math
import struct
from typing import Any

# Canonical name -> (struct format or None, size in bytes or None for variable).
_FIXED: dict[str, tuple[str, int]] = {
    "BOOL": ("<B", 1),
    "INT8": ("<b", 1),
    "UINT8": ("<B", 1),
    "INT16": ("<h", 2),
    "UINT16": ("<H", 2),
    "INT32": ("<i", 4),
    "UINT32": ("<I", 4),
    "INT64": ("<q", 8),
    "UINT64": ("<Q", 8),
    "REAL32": ("<f", 4),
    "REAL64": ("<d", 8),
}
_VARIABLE = ("STRING", "BYTES")

# Aliases used by CANopen / ETG ESI files and IEC 61131-3.
_ALIASES = {
    "BOOLEAN": "BOOL",
    "BIT": "BOOL",
    "SINT": "INT8",
    "INTEGER8": "INT8",
    "USINT": "UINT8",
    "BYTE": "UINT8",
    "UNSIGNED8": "UINT8",
    "INT": "INT16",
    "INTEGER16": "INT16",
    "UINT": "UINT16",
    "WORD": "UINT16",
    "UNSIGNED16": "UINT16",
    "DINT": "INT32",
    "INTEGER32": "INT32",
    "UDINT": "UINT32",
    "DWORD": "UINT32",
    "UNSIGNED32": "UINT32",
    "LINT": "INT64",
    "INTEGER64": "INT64",
    "ULINT": "UINT64",
    "LWORD": "UINT64",
    "UNSIGNED64": "UINT64",
    "REAL": "REAL32",
    "FLOAT": "REAL32",
    "LREAL": "REAL64",
    "DOUBLE": "REAL64",
    "VISIBLE_STRING": "STRING",
    "VISIBLESTRING": "STRING",
    "OCTET_STRING": "BYTES",
    "OCTETSTRING": "BYTES",
    "RAW": "BYTES",
}

DATA_TYPES = tuple(_FIXED) + _VARIABLE
_UINT_BY_SIZE = {1: "UINT8", 2: "UINT16", 4: "UINT32", 8: "UINT64"}


def parse_int(value: Any, what: str, minimum: int | None = None, maximum: int | None = None) -> int:
    """Parse an int given as a number or a string (``"0x1018"``, ``"#x1018"``, ``"4120"``)."""
    if isinstance(value, bool):
        raise ValueError(f"{what} must be an integer, not a boolean")
    if isinstance(value, int):
        number = value
    elif isinstance(value, float) and value.is_integer():
        number = int(value)
    elif isinstance(value, str):
        text = value.strip()
        if text.lower().startswith("#x"):  # ETG ESI notation
            text = "0x" + text[2:]
        try:
            number = int(text, 0)
        except ValueError:
            raise ValueError(f"{what} {value!r} is not a number (use decimal, or hex with a 0x prefix)") from None
    else:
        raise ValueError(f"{what} must be an integer, got {type(value).__name__}")
    if minimum is not None and number < minimum:
        raise ValueError(f"{what} {number} is below the minimum {minimum}")
    if maximum is not None and number > maximum:
        raise ValueError(f"{what} {number} is above the maximum {maximum:#x}")
    return number


def normalize_data_type(data_type: str | None) -> str | None:
    if data_type is None:
        return None
    if not isinstance(data_type, str) or not data_type.strip():
        raise ValueError(f"data_type must be one of {', '.join(DATA_TYPES)}")
    name = data_type.strip().upper().replace(" ", "_")
    name = _ALIASES.get(name, name)
    if name not in DATA_TYPES:
        raise ValueError(f"Unknown data_type {data_type!r} (use one of {', '.join(DATA_TYPES)})")
    return name


def parse_bytes(data: Any, what: str = "data") -> bytes:
    """Turn a list of ints (0..255) into bytes, rejecting anything else."""
    if not isinstance(data, list | tuple) or not data:
        raise ValueError(f"{what} must be a non-empty list of byte values (0-255)")
    out = bytearray()
    for i, item in enumerate(data):
        byte = parse_int(item, f"{what}[{i}]")
        if not 0 <= byte <= 0xFF:
            raise ValueError(f"{what}[{i}]={byte} is not a byte value (0-255)")
        out.append(byte)
    return bytes(out)


def encode_value(value: Any, data_type: str | None) -> tuple[bytes, str]:
    """Encode ``value`` for an SDO write. Returns ``(payload, data_type_used)``."""
    dtype = normalize_data_type(data_type)
    if dtype is None:
        if isinstance(value, list | tuple):
            return parse_bytes(value, "value"), "BYTES"
        raise ValueError(
            "data_type is required to encode a scalar value "
            f"(one of {', '.join(DATA_TYPES)}), or pass value as a list of byte values"
        )
    if dtype == "BYTES":
        if isinstance(value, str):
            text = value.strip().removeprefix("0x").replace(" ", "")
            try:
                payload = bytes.fromhex(text)
            except ValueError:
                raise ValueError(f"value {value!r} is not a hex string") from None
            if not payload:
                raise ValueError("value must not be empty")
            return payload, dtype
        return parse_bytes(value, "value"), dtype
    if dtype == "STRING":
        if not isinstance(value, str):
            raise ValueError("value must be a string for data_type STRING")
        return value.encode("utf-8"), dtype
    fmt, _size = _FIXED[dtype]
    if dtype == "BOOL":
        if isinstance(value, bool):
            return struct.pack(fmt, int(value)), dtype
        number = parse_int(value, "value")
        if number not in (0, 1):
            raise ValueError("value must be true/false or 0/1 for data_type BOOL")
        return struct.pack(fmt, number), dtype
    if dtype.startswith("REAL"):
        if isinstance(value, bool) or not isinstance(value, int | float | str):
            raise ValueError(f"value must be a number for data_type {dtype}")
        try:
            number_f = float(value)
        except ValueError:
            raise ValueError(f"value {value!r} is not a number") from None
        if not math.isfinite(number_f):
            raise ValueError("value must be a finite number")
        try:
            return struct.pack(fmt, number_f), dtype
        except (OverflowError, struct.error):
            raise ValueError(f"value {value!r} does not fit in {dtype}") from None
    number = parse_int(value, "value")
    try:
        return struct.pack(fmt, number), dtype
    except struct.error:
        raise ValueError(f"value {number} does not fit in {dtype}") from None


def decode_value(raw: bytes, data_type: str | None) -> dict[str, Any]:
    """Decode an SDO upload. Without a data type, sizes 1/2/4/8 are read as unsigned ints."""
    dtype = normalize_data_type(data_type)
    inferred = False
    if dtype is None:
        dtype = _UINT_BY_SIZE.get(len(raw))
        inferred = dtype is not None
    value: Any = None
    if dtype == "BYTES" or dtype is None:
        value = None if dtype is None else list(raw)
    elif dtype == "STRING":
        value = raw.split(b"\x00", 1)[0].decode("utf-8", errors="replace")
    else:
        fmt, size = _FIXED[dtype]
        if len(raw) != size:
            raise ValueError(f"Slave returned {len(raw)} bytes, but {dtype} needs {size}")
        value = struct.unpack(fmt, raw)[0]
        if dtype == "BOOL":
            value = bool(value)
    return {
        "value": value,
        "data_type": dtype,
        "data_type_inferred": inferred,
        "raw_hex": raw.hex(),
        "size": len(raw),
    }
