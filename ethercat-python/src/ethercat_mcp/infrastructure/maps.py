from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ethercat_mcp.codec import parse_int


class SlaveMap:
    """Aliases from ``SLAVE_MAP_FILE``, reloaded when the file changes."""

    def __init__(self, path: Path | None) -> None:
        self.path = path
        self._slaves: dict[str, Any] = {}
        self._stamp: tuple[int, int] | None = None
        self.error: str | None = None
        self.refresh()

    def refresh(self) -> None:
        if not self.path:
            self._slaves, self._stamp, self.error = ({}, None, None)
            return
        try:
            stat = self.path.stat()
        except OSError as exc:
            self._slaves, self._stamp = ({}, None)
            self.error = f"SLAVE_MAP_FILE {self.path} cannot be read: {exc.strerror or exc}"
            return
        stamp = (stat.st_mtime_ns, stat.st_size)
        if stamp == self._stamp:
            return
        self._stamp = stamp
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            self._slaves = {}
            self.error = f"SLAVE_MAP_FILE {self.path} is not valid JSON: {exc}"
            return
        if not isinstance(data, dict):
            self._slaves = {}
            self.error = f"SLAVE_MAP_FILE {self.path} must hold a JSON object of alias -> {{position, offset}}"
            return
        self._slaves = {str(alias): spec for alias, spec in data.items()}
        self.error = None

    def count(self) -> int:
        self.refresh()
        return len(self._slaves)

    def resolve(self, alias: str) -> tuple[int, int, int | None]:
        """Return ``(position, offset, length)`` for ``alias``; raise ``ValueError`` with the reason."""
        self.refresh()
        if self.error:
            raise ValueError(self.error)
        if alias not in self._slaves:
            raise ValueError(f"Unknown alias {alias!r}")
        spec = self._slaves[alias]
        if not isinstance(spec, dict):
            raise ValueError(f"Alias {alias!r} in SLAVE_MAP_FILE must be an object with a position")
        if "position" not in spec:
            raise ValueError(f"Alias {alias!r} in SLAVE_MAP_FILE has no position")
        position = parse_int(spec["position"], f"position of alias {alias!r}", minimum=0)
        offset = parse_int(spec.get("offset", 0), f"offset of alias {alias!r}", minimum=0)
        length = spec.get("length")
        length = None if length is None else parse_int(length, f"length of alias {alias!r}", minimum=1)
        return (position, offset, length)

    def list(self) -> list[dict[str, Any]]:
        self.refresh()
        entries = []
        for alias, spec in self._slaves.items():
            entry: dict[str, Any] = {"alias": alias}
            if isinstance(spec, dict):
                entry.update(
                    {
                        "position": spec.get("position"),
                        "offset": spec.get("offset", 0),
                        "length": spec.get("length"),
                        "description": spec.get("description"),
                        "vendor_id": spec.get("vendor_id"),
                    }
                )
            try:
                self.resolve(alias)
            except ValueError as exc:
                entry["error"] = str(exc)
            entries.append(entry)
        return entries
