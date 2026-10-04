"""Mock transport: talks to ``ethercat-mock-slave`` over newline-delimited JSON.

The mock cannot speak raw EtherCAT, so with ``ETHERCAT_BACKEND=mock`` the
server sends one JSON request per TCP connection to
``ETHERCAT_MOCK_HOST:ETHERCAT_MOCK_PORT`` instead of opening a network
interface. The mock simulates the slaves, their AL state machine, process data
and CoE objects. Every tool response produced through this backend carries
``meta.backend = "mock"`` and ``meta.simulated = true``.
"""

from __future__ import annotations

import json
import socket
import threading
from typing import Any

from .config import EthercatConfig
from .ec_master import EthercatMasterError, check_slice, refuse_rescan, state_name

_MAX_REPLY = 1 << 20


class MockBridgeBackend:
    name = "mock"

    def __init__(self, config: EthercatConfig) -> None:
        self.config = config
        self._lock = threading.Lock()
        self._slaves: list[dict[str, Any]] = []
        self._scanned = False
        self._requests = 0

    @property
    def endpoint(self) -> str:
        return f"{self.config.mock_host}:{self.config.mock_port}"

    def _request(self, payload: dict[str, Any]) -> Any:
        timeout = self.config.timeout_us / 1_000_000
        try:
            with socket.create_connection((self.config.mock_host, self.config.mock_port), timeout=timeout) as sock:
                sock.sendall(json.dumps(payload).encode("utf-8") + b"\n")
                buf = bytearray()
                while not buf.endswith(b"\n"):
                    chunk = sock.recv(65536)
                    if not chunk:
                        break
                    buf += chunk
                    if len(buf) > _MAX_REPLY:
                        raise EthercatMasterError(f"Mock slave at {self.endpoint} sent an oversized reply")
        except OSError as exc:
            raise EthercatMasterError(f"Mock slave at {self.endpoint} is not reachable: {exc}") from exc
        self._requests += 1
        try:
            reply = json.loads(buf)
        except ValueError:
            raise EthercatMasterError(f"Mock slave at {self.endpoint} sent invalid JSON") from None
        if not isinstance(reply, dict) or not isinstance(reply.get("success"), bool):
            raise EthercatMasterError(f"Mock slave at {self.endpoint} sent an unexpected reply")
        if not reply["success"]:
            raise EthercatMasterError(str(reply.get("error") or "Mock slave reported an error"))
        return reply.get("data")

    def _hex_field(self, data: Any, key: str) -> bytes:
        try:
            return bytes.fromhex(data[key])
        except (KeyError, TypeError, ValueError):
            raise EthercatMasterError(f"Mock slave at {self.endpoint} sent a reply without a valid {key!r}") from None

    def _require_scanned(self) -> None:
        if not self._scanned:
            raise EthercatMasterError("No bus scan yet: call scan_network first")

    def _require_position(self, position: int) -> None:
        self._require_scanned()
        if not 0 <= position < len(self._slaves):
            raise EthercatMasterError(
                f"Slave position {position} is out of range (the last scan found {len(self._slaves)} slave(s))"
            )

    @staticmethod
    def _entry(raw: dict[str, Any]) -> dict[str, Any]:
        code = int(raw.get("state_code", 0))
        return {
            "position": int(raw["position"]),
            "name": raw.get("name"),
            "vendor_id": raw.get("vendor_id"),
            "product_code": raw.get("product_code"),
            "revision": raw.get("revision"),
            "input_size": raw.get("input_size"),
            "output_size": raw.get("output_size"),
            "state": state_name(code),
            "state_code": code,
            "al_status_code": f"0x{int(raw.get('al_status_code', 0)):04X}",
            "al_status": raw.get("al_status", ""),
        }

    @staticmethod
    def _state(raw: dict[str, Any]) -> dict[str, Any]:
        code = int(raw.get("state_code", 0))
        return {
            "position": int(raw["position"]),
            "state": state_name(code),
            "state_code": code,
            "al_status_code": f"0x{int(raw.get('al_status_code', 0)):04X}",
            "al_status": raw.get("al_status", ""),
        }

    # -- Backend API ----------------------------------------------------------

    @property
    def scanned(self) -> bool:
        return self._scanned

    def describe(self) -> dict[str, Any]:
        return {"backend": self.name, "simulated": True, "mock_endpoint": self.endpoint}

    def scan(self) -> list[dict[str, Any]]:
        with self._lock:
            if self._scanned:
                refuse_rescan(self._read_states())
            data = self._request({"op": "scan"})
            if not isinstance(data, list):
                raise EthercatMasterError("Mock slave sent an unexpected scan reply")
            try:
                self._slaves = [self._entry(item) for item in data]
            except (KeyError, TypeError, ValueError):
                raise EthercatMasterError("Mock slave sent an unexpected scan reply") from None
            self._scanned = True
            return [dict(slave) for slave in self._slaves]

    def slave_count(self) -> int:
        return len(self._slaves)

    def slave_entry(self, position: int) -> dict[str, Any]:
        with self._lock:
            self._require_position(position)
            return dict(self._slaves[position])

    def _read_states(self) -> list[dict[str, Any]]:
        data = self._request({"op": "read_state"})
        try:
            states = [self._state(item) for item in data]
        except (KeyError, TypeError, ValueError):
            raise EthercatMasterError(f"Mock slave at {self.endpoint} sent an unexpected state reply") from None
        for state in states:
            pos = state["position"]
            if 0 <= pos < len(self._slaves):
                self._slaves[pos].update(state)
        return states

    def read_states(self) -> list[dict[str, Any]]:
        with self._lock:
            self._require_scanned()
            return self._read_states()

    def read_pdo(self, position: int, offset: int, length: int | None) -> tuple[bytes, dict[str, Any]]:
        with self._lock:
            self._require_position(position)
            data = self._request({"op": "read_pdo", "position": position, "offset": 0})
        image = self._hex_field(data, "raw_data_hex")
        return check_slice(image, offset, length, "input", position), {"state": data.get("state")}

    def write_pdo(self, position: int, offset: int, data: bytes) -> dict[str, Any]:
        with self._lock:
            self._require_position(position)
            reply = self._request({"op": "write_pdo", "position": position, "offset": offset, "data": list(data)})
        return {"state": reply.get("state"), "written_bytes": reply.get("written_bytes")}

    def read_sdo(self, position: int, index: int, subindex: int) -> bytes:
        with self._lock:
            self._require_position(position)
            data = self._request({"op": "read_sdo", "position": position, "index": index, "subindex": subindex})
        return self._hex_field(data, "data_hex")

    def write_sdo(self, position: int, index: int, subindex: int, data: bytes) -> None:
        with self._lock:
            self._require_position(position)
            self._request(
                {"op": "write_sdo", "position": position, "index": index, "subindex": subindex, "data_hex": data.hex()}
            )

    def set_state(self, position: int, state: int) -> dict[str, Any]:
        with self._lock:
            self._require_position(position)
            data = self._request({"op": "set_state", "position": position, "state_code": state})
            try:
                entry = self._state(data)
            except (KeyError, TypeError, ValueError):
                raise EthercatMasterError(f"Mock slave at {self.endpoint} sent an unexpected state reply") from None
            self._slaves[position].update(entry)
        return {"requested": state_name(state), **entry}

    def status(self) -> dict[str, Any]:
        return {
            "mock_endpoint": self.endpoint,
            "timeout_us": self.config.timeout_us,
            "scanned": self._scanned,
            "slaves": len(self._slaves),
            "requests": self._requests,
        }

    def close(self) -> None:
        with self._lock:
            self._slaves = []
            self._scanned = False
