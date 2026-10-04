"""Mock transport: talks to ``ethercat-mock-slave`` over newline-delimited JSON.

The mock cannot speak raw EtherCAT, so with ``ETHERCAT_BACKEND=mock`` the
server sends one JSON request per TCP connection to
``ETHERCAT_MOCK_HOST:ETHERCAT_MOCK_PORT`` instead of opening a network
interface. The mock simulates the slaves, their AL state machine, the output
process image, process data and CoE objects. This backend applies the same
safety rules as the PySOEM backend (live-segment check before a scan, zeroed
output image after a scan and around OP). Every tool response produced through
it carries ``meta.backend = "mock"`` and ``meta.simulated = true``.

Requests are never repeated: a device-changing request whose reply is lost is
reported with outcome "unknown".
"""

from __future__ import annotations

import json
import socket
import threading
import time
from typing import Any

from .config import EthercatConfig
from .ec_master import (
    OP_STATE,
    REJECTED,
    WRITTEN,
    CallToken,
    EthercatMasterError,
    _token,
    check_slice,
    locked,
    refuse_live_segment,
    refuse_rescan,
    state_name,
    unknown_outcome,
)

_MAX_REPLY = 1 << 20


class MockBridgeBackend:
    name = "mock"

    def __init__(self, config: EthercatConfig) -> None:
        self.config = config
        self._lock = threading.Lock()
        self._slaves: list[dict[str, Any]] = []
        self._scanned = False
        self._requests = 0
        self.last_scan: dict[str, Any] = {}

    @property
    def endpoint(self) -> str:
        return f"{self.config.mock_host}:{self.config.mock_port}"

    def _request(
        self,
        payload: dict[str, Any],
        *,
        write: bool = False,
        token: CallToken | None = None,
        send_point: bool = True,
    ) -> Any:
        """One request, one connection, no retries.

        For a device-changing request (``write``), a failure after the request
        went out is an "unknown" outcome and an error reply is "rejected".
        ``token`` is checked once connected, immediately before the request goes
        out: a cancelled call never sends. ``send_point=False`` marks a write that
        does not count as sending the call's command (clearing the output image of
        a slave that is not in OP).
        """
        token = _token(token)
        timeout = self.config.timeout_us / 1_000_000
        remaining = token.remaining()
        if remaining is not None:
            timeout = max(0.001, min(timeout, remaining))
        what = payload.get("op")
        try:
            request = json.dumps(payload).encode("utf-8") + b"\n"  # built before anything is sent
        except (TypeError, ValueError) as exc:
            raise EthercatMasterError(f"Could not encode the {what} request: {exc}") from exc
        try:
            sock = socket.create_connection((self.config.mock_host, self.config.mock_port), timeout=timeout)
        except OSError as exc:
            raise EthercatMasterError(f"Mock slave at {self.endpoint} is not reachable: {exc}") from exc
        # One overall deadline for the whole exchange: a peer that trickles bytes, or answers
        # garbage without a newline, cannot keep the call alive (suite rule 12).
        deadline = time.monotonic() + timeout
        try:
            with sock:
                if write and send_point:
                    token.begin_send()  # the last moment to decide: after this, it may arrive
                else:
                    token.check()
                sock.sendall(request)
                buf = bytearray()
                while not buf.endswith(b"\n"):
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError(f"no complete reply within {timeout:.3f} s")
                    sock.settimeout(remaining)
                    chunk = sock.recv(65536)
                    if not chunk:
                        break
                    buf += chunk
                    if len(buf) > _MAX_REPLY:
                        raise OSError("oversized reply")
            self._requests += 1
            reply = json.loads(buf)
            if not isinstance(reply, dict) or not isinstance(reply.get("success"), bool):
                raise ValueError("unexpected reply")
        except EthercatMasterError:
            raise  # the token stopped the call before sendall
        except Exception as exc:  # anything after the request may have gone out
            message = f"Mock slave at {self.endpoint} gave no usable reply to {what}: {type(exc).__name__}: {exc}"
            if write:
                raise unknown_outcome(message) from exc
            raise EthercatMasterError(message) from exc
        if not reply["success"]:
            error = str(reply.get("error") or "Mock slave reported an error")
            if write and error.startswith("Internal mock error"):
                # The mock failed while handling the request: it may have been applied in part.
                raise unknown_outcome(error)
            # The mock checks every request before it applies anything, so its refusals are
            # pre-execution: "rejected".
            raise EthercatMasterError(error, outcome=REJECTED if write else None)
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

    def _read_states(self, token: CallToken | None = None) -> list[dict[str, Any]]:
        data = self._request({"op": "read_state"}, token=token)
        try:
            states = [self._state(item) for item in data]
        except (KeyError, TypeError, ValueError):
            raise EthercatMasterError(f"Mock slave at {self.endpoint} sent an unexpected state reply") from None
        for state in states:
            pos = state["position"]
            if 0 <= pos < len(self._slaves):
                self._slaves[pos].update(state)
        return states

    def _clear_outputs(self, position: int, token: CallToken | None = None) -> None:
        """Zero this slave's part of the output image (the mock keeps it, like SOEM's io_map)."""
        if self._slaves[position].get("output_size"):
            self._request({"op": "clear_outputs", "position": position}, write=True, token=token, send_point=False)

    # -- Backend API ----------------------------------------------------------

    @property
    def scanned(self) -> bool:
        return self._scanned

    def describe(self) -> dict[str, Any]:
        return {"backend": self.name, "simulated": True, "mock_endpoint": self.endpoint}

    def scan(self, token: CallToken | None = None) -> list[dict[str, Any]]:
        token = _token(token)
        with locked(self._lock, token):
            states = self._read_states(token)
            if self._scanned:
                refuse_rescan(states)
            bits = 0
            for state in states:
                bits |= state["state_code"]
            bits &= 0x0F
            refuse_live_segment(bits, self.config.state_changes_allowed)
            self.last_scan = {
                "segment_state_before": state_name(bits) if bits else "NONE (no answer)",
                "reset_live_segment": bool(bits & 0x0C),
            }
            data = self._request({"op": "scan"}, write=True, token=token)
            try:
                if not isinstance(data, list):
                    raise TypeError("not a list")
                self._slaves = [self._entry(item) for item in data]
            except (KeyError, TypeError, ValueError):
                raise unknown_outcome("Mock slave sent an unexpected scan reply") from None
            self._scanned = True
            # Like zeroing the image after config_map: start from zero, whatever an earlier run left.
            for position in range(len(self._slaves)):
                self._clear_outputs(position, token)
            return [dict(slave) for slave in self._slaves]

    def slave_count(self) -> int:
        return len(self._slaves)

    def slave_entry(self, position: int, token: CallToken | None = None) -> dict[str, Any]:
        with locked(self._lock, _token(token)):
            self._require_position(position)
            return dict(self._slaves[position])

    def read_states(self, token: CallToken | None = None) -> list[dict[str, Any]]:
        token = _token(token)
        with locked(self._lock, token):
            self._require_scanned()
            return self._read_states(token)

    def read_pdo(
        self, position: int, offset: int, length: int | None, token: CallToken | None = None
    ) -> tuple[bytes, dict[str, Any]]:
        token = _token(token)
        with locked(self._lock, token):
            self._require_position(position)
            data = self._request({"op": "read_pdo", "position": position, "offset": 0}, token=token)
        image = self._hex_field(data, "raw_data_hex")
        return check_slice(image, offset, length, "input", position), {"state": data.get("state")}

    def write_pdo(self, position: int, offset: int, data: bytes, token: CallToken | None = None) -> dict[str, Any]:
        token = _token(token)
        with locked(self._lock, token):
            self._require_position(position)
            reply = self._request(
                {"op": "write_pdo", "position": position, "offset": offset, "data": list(data)},
                write=True,
                token=token,
            )
        return {
            "state": reply.get("state"),
            "written_bytes": reply.get("written_bytes"),
            "verified": "mock_reply",
            "outcome": WRITTEN,
        }

    def read_sdo(self, position: int, index: int, subindex: int, token: CallToken | None = None) -> bytes:
        token = _token(token)
        with locked(self._lock, token):
            self._require_position(position)
            data = self._request(
                {"op": "read_sdo", "position": position, "index": index, "subindex": subindex}, token=token
            )
        return self._hex_field(data, "data_hex")

    def write_sdo(self, position: int, index: int, subindex: int, data: bytes, token: CallToken | None = None) -> None:
        token = _token(token)
        with locked(self._lock, token):
            self._require_position(position)
            self._request(
                {"op": "write_sdo", "position": position, "index": index, "subindex": subindex, "data_hex": data.hex()},
                write=True,
                token=token,
            )

    def set_state(self, position: int, state: int, token: CallToken | None = None) -> dict[str, Any]:
        token = _token(token)
        with locked(self._lock, token):
            self._require_position(position)
            current = self._read_states(token)[position]["state_code"]
            if state == OP_STATE and current != OP_STATE:
                self._clear_outputs(position, token)  # enter OP with zero outputs, never stale ones
            try:
                data = self._request(
                    {"op": "set_state", "position": position, "state_code": state}, write=True, token=token
                )
            finally:
                try:
                    if self._read_states(token)[position]["state_code"] != OP_STATE:
                        self._clear_outputs(position, token)  # outside OP: never leave stale outputs for later
                except EthercatMasterError:
                    pass  # best effort; the request's own outcome is what gets reported
            try:
                entry = self._state(data)
            except (KeyError, TypeError, ValueError):
                raise unknown_outcome(f"Mock slave at {self.endpoint} sent an unexpected state reply") from None
            self._slaves[position].update(entry)
        return {"requested": state_name(state), "outcome": WRITTEN, **entry}

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
