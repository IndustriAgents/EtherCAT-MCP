"""Read a bounded policy document from a regular file, without opening streams."""

from __future__ import annotations

import json
import os
import stat

MAX_POLICY_BYTES = 65536
MAX_POLICY_NESTING = 16


def _unique_fields(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Policy file contains duplicate fields")
        result[key] = value
    return result


def _check_nesting(text):
    # Bound JSON parser work independently of process-wide recursion settings.
    depth = 0
    quoted = escaped = False
    for char in text:
        if quoted:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                quoted = False
        elif char == '"':
            quoted = True
        elif char in "{[":
            depth += 1
            if depth > MAX_POLICY_NESTING:
                raise ValueError("Policy file contains excessive nesting")
        elif char in "}]":
            depth -= 1


def read_policy_document(filename):
    fd = os.open(filename, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0))
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode):
            raise ValueError("Policy path must refer to a regular file")
        if info.st_size > MAX_POLICY_BYTES:
            raise ValueError("Policy file exceeds 64 KiB")
        # A file can grow after fstat. Never perform an unbounded read.
        raw = stream.read(MAX_POLICY_BYTES + 1)
    if len(raw) > MAX_POLICY_BYTES:
        raise ValueError("Policy file exceeds 64 KiB")
    text = raw.decode("utf-8")
    _check_nesting(text)
    try:
        return json.loads(text, object_pairs_hook=_unique_fields)
    except RecursionError as exc:
        raise ValueError("Policy file contains excessive nesting") from exc
