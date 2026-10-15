"""Append-only, fsynced audit. Payload values and credentials are never recorded."""

from __future__ import annotations

import json
import os
import time
from datetime import UTC, datetime


class Audit:
    def __init__(self, path=None):
        self.path = path

    def record(self, ticket, phase, result):
        if self.path is None:
            return
        meta = (result or {}).get("meta", {})
        entry = {
            "timestamp": datetime.now(UTC).isoformat(),
            "call_id": ticket.call_id,
            "tool": ticket.tool,
            "operation": ticket.operation,
            "phase": phase,
            "elapsed_ms": round((time.monotonic() - ticket.started) * 1000, 3),
        }
        if phase == "result":
            entry.update(
                success=(result or {}).get("success"),
                outcome=meta.get("outcome", "unknown" if result is None else None),
                request_sent=meta.get("request_sent"),
            )
        data = (json.dumps(entry, separators=(",", ":")) + "\n").encode()
        flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(self.path, flags, 0o600)
        try:
            view = memoryview(data)
            while view:
                written = os.write(fd, view)
                if written <= 0:
                    raise OSError("Audit write made no progress")
                view = view[written:]
            os.fsync(fd)
        finally:
            os.close(fd)
