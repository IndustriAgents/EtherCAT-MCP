from __future__ import annotations

from typing import Any

MAX_PDO_BYTES = 4096
ESI_DEADLINE_S = 60.0


def envelope(success: bool, data: Any = None, error: str | None = None, meta: dict[str, Any] | None = None) -> dict:
    return {"success": success, "data": data, "error": error, "meta": meta or {}}
