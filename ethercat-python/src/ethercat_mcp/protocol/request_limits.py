"""Bound decoded arguments before schema validation, coercion or device work."""

from __future__ import annotations

import json
import math
from typing import Any


class RequestError(ValueError):
    def __init__(self, message: str, code: str = "invalid_arguments"):
        super().__init__(message)
        self.code = code


def validate_request(arguments: Any, max_bytes: int, limits: dict[str, int]) -> None:
    """Iteratively bound work and then check the exact serialized byte count.

    The iterator stack grows with depth, not request width. A cheap byte lower
    bound rejects oversized strings before encoding them. The SDK has already
    decoded the transport frame; these limits bound subsequent application work.
    """
    if not isinstance(arguments, dict):
        raise RequestError("Arguments must be an object")
    stack = [(iter((arguments,)), 0)]
    total = 0
    minimum_bytes = 0

    def account_string(value: str) -> None:
        nonlocal minimum_bytes
        minimum_bytes += len(value)
        if minimum_bytes > max_bytes:
            raise RequestError("Request exceeds max_request_bytes", "resource_limit")
        try:
            value.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise RequestError("Arguments must contain valid Unicode strings") from exc

    while stack:
        values, depth = stack[-1]
        try:
            value = next(values)
        except StopIteration:
            stack.pop()
            continue
        total += 1
        if total > limits["maxTotalValues"]:
            raise RequestError("Request exceeds maxTotalValues", "resource_limit")
        if depth > limits["maxNestingDepth"]:
            raise RequestError("Request exceeds maxNestingDepth", "resource_limit")
        if isinstance(value, dict):
            if len(value) > limits["maxContainerItems"]:
                raise RequestError("Request exceeds maxContainerItems", "resource_limit")
            for key in value:
                if not isinstance(key, str):
                    raise RequestError("Argument object keys must be strings")
                account_string(key)
            minimum_bytes += 2 + len(value)
            stack.append((iter(value.values()), depth + 1))
        elif isinstance(value, list):
            if len(value) > limits["maxContainerItems"]:
                raise RequestError("Request exceeds maxContainerItems", "resource_limit")
            minimum_bytes += 2 + len(value)
            stack.append((iter(value), depth + 1))
        elif isinstance(value, str):
            account_string(value)
        elif value is None or isinstance(value, bool):
            minimum_bytes += 4
        elif isinstance(value, int):
            minimum_bytes += max(1, value.bit_length() // 4)
        elif isinstance(value, float) and math.isfinite(value):
            minimum_bytes += 1
        else:
            raise RequestError("Arguments must contain finite JSON values")
        if minimum_bytes > max_bytes:
            raise RequestError("Request exceeds max_request_bytes", "resource_limit")

    try:
        encoded = json.dumps(arguments, allow_nan=False).encode("utf-8")
    except (ValueError, TypeError, RecursionError) as exc:
        raise RequestError("Arguments must contain finite JSON values") from exc
    if len(encoded) > max_bytes:
        raise RequestError("Request exceeds max_request_bytes", "resource_limit")
