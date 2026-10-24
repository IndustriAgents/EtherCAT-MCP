"""The mock transport on its own: outcomes, deadlines and output clearing.

Scripted TCP peers stand in for a misbehaving mock (suite rules 1a and 12); the
real mock is used for the output-image clearing (review round 2, finding 3).
"""

from __future__ import annotations

import json
import socket
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager

import pytest
from conftest import running_mock

from ethercat_mcp.config import EthercatConfig
from ethercat_mcp.ec_master import OP_STATE, SAFEOP_STATE, EthercatMasterError
from ethercat_mcp.mock_bridge import MockBridgeBackend

GATES = {"ETHERCAT_WRITES_ENABLED": "true", "ETHERCAT_STATE_CHANGE_ENABLED": "true"}


def bridge(port: int, **env: str) -> MockBridgeBackend:
    settings = {"ETHERCAT_BACKEND": "mock", "ETHERCAT_MOCK_PORT": str(port), "ETHERCAT_TIMEOUT": "300000", **env}
    return MockBridgeBackend(EthercatConfig.from_env(settings))


def scanned(backend: MockBridgeBackend) -> MockBridgeBackend:
    backend._scanned = True
    backend._slaves = [{"position": i, "output_size": 1} for i in range(4)]
    return backend


@contextmanager
def peer(behaviour: Callable[[socket.socket], None]) -> Iterator[int]:
    """A one-connection-at-a-time TCP peer on a free loopback port."""
    server = socket.create_server(("127.0.0.1", 0))
    server.settimeout(0.2)
    stop = threading.Event()

    def serve() -> None:
        while not stop.is_set():
            try:
                conn, _ = server.accept()
            except TimeoutError:
                continue
            with conn:
                try:
                    behaviour(conn)
                except OSError:
                    pass

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    try:
        yield server.getsockname()[1]
    finally:
        stop.set()
        thread.join(timeout=5)
        server.close()


def read_line(conn: socket.socket) -> bytes:
    buf = b""
    while not buf.endswith(b"\n"):
        chunk = conn.recv(4096)
        if not chunk:
            break
        buf += chunk
    return buf


def hang_up(conn: socket.socket) -> None:
    read_line(conn)  # took the request, then the link drops


def garbage_trickle(conn: socket.socket) -> None:
    read_line(conn)
    for _ in range(100):  # bytes without a newline, slowly: no complete reply ever
        conn.sendall(b"\x00garbage")
        time.sleep(0.05)


def reply(message: dict) -> Callable[[socket.socket], None]:
    def answer(conn: socket.socket) -> None:
        read_line(conn)
        conn.sendall(json.dumps(message).encode() + b"\n")

    return answer


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


# -- outcomes (suite rule 1a) ------------------------------------------------------


@pytest.mark.parametrize("data", [None, [], {}, {"written_bytes": 0}, {"written_bytes": True}])
@pytest.mark.parametrize("operation", ["write_pdo", "write_sdo"])
def test_invalid_or_partial_write_acknowledgement_is_unknown(data, operation):
    received = []

    def answer(conn):
        received.append(json.loads(read_line(conn)))
        conn.sendall(json.dumps({"success": True, "data": data}).encode() + b"\n")

    with peer(answer) as port:
        backend = scanned(bridge(port))
        with pytest.raises(EthercatMasterError) as caught:
            if operation == "write_pdo":
                backend.write_pdo(2, 0, b"\x01")
            else:
                backend.write_sdo(2, 0x2000, 0, b"\x01")
        assert caught.value.outcome == "unknown" and caught.value.request_sent is True
    assert len(received) == 1


def test_scan_cleanup_failure_cannot_claim_the_scan_was_not_sent(monkeypatch):
    backend = bridge(1)
    requests = []

    def request(payload, **kwargs):
        requests.append(payload["op"])
        if payload["op"] == "read_state":
            return [{"position": 0, "state_code": 2}]
        assert payload["op"] == "scan"
        return [{"position": 0, "state_code": 2, "output_size": 1}]

    def failed_cleanup(*args):
        raise EthercatMasterError("cleanup connection failed before sending")

    monkeypatch.setattr(backend, "_request", request)
    monkeypatch.setattr(backend, "_clear_outputs", failed_cleanup)
    with pytest.raises(EthercatMasterError) as caught:
        backend.scan()
    assert caught.value.outcome == "unknown" and caught.value.request_sent is True
    assert requests == ["read_state", "scan"]


def test_lost_reply_to_a_write_is_unknown() -> None:
    with peer(hang_up) as port:
        backend = scanned(bridge(port))
        with pytest.raises(EthercatMasterError, match="may have been applied") as info:
            backend.write_sdo(3, 0x8000, 6, b"\x01")
        assert info.value.outcome == "unknown" and info.value.request_sent
        with pytest.raises(EthercatMasterError) as info:
            backend.read_sdo(3, 0x8000, 6)
        assert info.value.outcome == "not_sent"  # a read has no outcome to report


def test_refused_connection_is_not_sent() -> None:
    backend = scanned(bridge(free_port()))
    with pytest.raises(EthercatMasterError, match="not reachable") as info:
        backend.write_pdo(2, 0, b"\x01")
    assert info.value.outcome == "not_sent"


def test_error_reply_is_rejected_but_an_internal_mock_error_is_unknown() -> None:
    with peer(reply({"success": False, "error": "SDO abort 0x06010002 on 0x1000:0"})) as port:
        with pytest.raises(EthercatMasterError) as info:
            scanned(bridge(port)).write_sdo(3, 0x1000, 0, b"\x00")
        assert info.value.outcome == "rejected"
    with peer(reply({"success": False, "error": "Internal mock error: boom"})) as port:
        with pytest.raises(EthercatMasterError) as info:
            scanned(bridge(port)).write_sdo(3, 0x8000, 6, b"\x01")
        assert info.value.outcome == "unknown"


# -- deadline (suite rule 12) --------------------------------------------------------


def test_a_peer_answering_garbage_cannot_hang_a_call() -> None:
    with peer(garbage_trickle) as port:
        backend = scanned(bridge(port))
        start = time.monotonic()
        with pytest.raises(EthercatMasterError, match="no usable reply") as info:
            backend.write_pdo(2, 0, b"\x01")
        assert info.value.outcome == "unknown"
        with pytest.raises(EthercatMasterError, match="no usable reply"):
            backend.read_pdo(1, 0, None)
        assert time.monotonic() - start < 3.0  # two calls, each bounded by ETHERCAT_TIMEOUT (0.3 s)


# -- output image clearing (review round 2, finding 3) ---------------------------------


def raw(port: int, request: dict) -> dict:
    with socket.create_connection(("127.0.0.1", port), timeout=5) as sock:
        sock.sendall(json.dumps(request).encode() + b"\n")
        return json.loads(read_line(sock))


@pytest.fixture
def mock_port() -> Iterator[int]:
    with running_mock() as port:
        yield port


def test_entering_op_clears_outputs_left_after_a_drop_the_server_did_not_see(mock_port: int) -> None:
    backend = bridge(mock_port, **GATES)
    backend.scan()
    backend.set_state(2, SAFEOP_STATE)
    backend.set_state(2, OP_STATE)
    backend.write_pdo(2, 0, b"\x3c")
    # The slave leaves OP behind the server's back (e.g. its watchdog): nothing cleared the image.
    raw(mock_port, {"op": "set_state", "position": 2, "state": "SAFEOP"})
    assert raw(mock_port, {"op": "read_outputs", "position": 2})["data"]["raw_data_hex"] == "3c"
    backend.set_state(2, OP_STATE)
    outputs = raw(mock_port, {"op": "read_outputs", "position": 2})["data"]
    assert outputs["applied_hex"] == "00" and outputs["raw_data_hex"] == "00"


def test_scan_clears_outputs_an_earlier_run_left_in_the_image(mock_port: int) -> None:
    raw(mock_port, {"op": "scan"})
    for state in ("SAFEOP", "OP"):
        raw(mock_port, {"op": "set_state", "position": 2, "state": state})
    raw(mock_port, {"op": "write_pdo", "position": 2, "data": [0x5A]})
    raw(mock_port, {"op": "set_state", "position": 2, "state": "INIT"})
    backend = bridge(mock_port)  # read-only server: it must still not leave the 0x5A behind
    backend.scan()
    assert raw(mock_port, {"op": "read_outputs", "position": 2})["data"]["raw_data_hex"] == "00"
