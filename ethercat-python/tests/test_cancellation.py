"""A write that never got to send is not_sent, and is never sent later (suite rules 1 and 12,
and the clarification on client cancellation and disconnects).

In process: a call queued behind the backend lock is cancelled (cancel scope) or runs out
of time, then the lock frees, and the device must not see it. Over stdio: the client
cancels a queued call (notifications/cancelled) or closes stdin, and a logging proxy in
front of the mock must never see the request arrive.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import anyio
import pytest
from conftest import running_mock, server_command
from fake_pysoem import FakePysoem

from ethercat_mcp.config import EthercatConfig
from ethercat_mcp.ec_master import OP_STATE, SAFEOP_STATE, EthercatMaster, EthercatMasterError, PysoemBackend

GATES = {"ETHERCAT_WRITES_ENABLED": "true", "ETHERCAT_STATE_CHANGE_ENABLED": "true"}


def master_with_fake(**env: str) -> tuple[EthercatMaster, PysoemBackend, FakePysoem]:
    config = EthercatConfig.from_env({"ETHERCAT_INTERFACE": "fake0", "ETHERCAT_TIMEOUT": "100000", **GATES, **env})
    fake = FakePysoem()
    backend = PysoemBackend(config, pysoem_module=fake)
    backend.scan()
    backend.set_state(2, SAFEOP_STATE)
    backend.set_state(2, OP_STATE)
    return EthercatMaster(config, backend=backend), backend, fake


@contextmanager
def lock_held(backend: PysoemBackend) -> Iterator[threading.Event]:
    """Another call (a slow EEPROM read, a CoE mapping, ...) holds the backend lock."""
    release = threading.Event()
    acquired = threading.Event()

    def hold() -> None:
        with backend._lock:
            acquired.set()
            release.wait(10)

    thread = threading.Thread(target=hold, daemon=True)
    thread.start()
    acquired.wait(5)
    try:
        yield release
    finally:
        release.set()
        thread.join(5)


@pytest.mark.anyio
async def test_cancelled_queued_writes_are_never_sent() -> None:
    master, backend, fake = master_with_fake()
    try:
        calls_before = len(fake.bus.calls)
        with lock_held(backend):
            with anyio.move_on_after(0.3):
                await master.write_pdo(2, 0, b"\xa5")
            with anyio.move_on_after(0.3):
                await master.write_sdo(3, 0x8000, 6, b"\x01")
            with anyio.move_on_after(0.3):
                await master.set_slave_state(1, SAFEOP_STATE)
        time.sleep(0.5)  # the lock is free again: give abandoned worker threads every chance
        assert fake.device(2).applied_outputs == b"\x00"
        assert backend._slaves[2].output == b"\x00"
        assert fake.device(3).objects[(0x8000, 6)][0] == b"\x00"
        assert not any(call[0] == "slave.write_state" for call in fake.bus.calls[calls_before:])
        assert fake.device(1).actual == 0x02  # still PREOP
    finally:
        await master.close()


@pytest.mark.anyio
async def test_a_write_that_times_out_while_queued_is_not_sent_and_never_sent_later(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(EthercatConfig, "call_deadline_s", property(lambda self: 0.4))
    master, backend, fake = master_with_fake()
    try:
        with lock_held(backend):
            with pytest.raises(EthercatMasterError, match="will not be sent later") as info:
                await master.write_pdo(2, 0, b"\xff")
            assert info.value.outcome == "not_sent"
        time.sleep(0.5)
        assert fake.device(2).applied_outputs == b"\x00"
    finally:
        await master.close()


@pytest.mark.anyio
async def test_after_shutdown_no_call_sends() -> None:
    master, backend, fake = master_with_fake()
    try:
        results: list[BaseException] = []
        with lock_held(backend):
            # The queued call must end promptly, while the bus is still busy, not when it frees.
            with anyio.fail_after(2):
                async with anyio.create_task_group() as tg:

                    async def queued() -> None:
                        try:
                            await master.write_sdo(3, 0x8000, 6, b"\x01")
                        except EthercatMasterError as exc:
                            results.append(exc)

                    tg.start_soon(queued)
                    await anyio.sleep(0.2)
                    master.shutdown_calls("the MCP client disconnected (stdin closed)")
            assert backend._lock.acquire(blocking=False) is False  # still held by the other call
        assert results and results[0].outcome == "not_sent" and "disconnected" in str(results[0])
        with pytest.raises(EthercatMasterError, match="Not sent: the MCP client disconnected"):
            await master.write_sdo(3, 0x8000, 6, b"\x01")
        assert fake.device(3).objects[(0x8000, 6)][0] == b"\x00"
    finally:
        await master.close()


# -- over stdio, against the mock behind a logging proxy ----------------------------------


class LoggingProxy:
    """Forwards to the mock and logs every request that arrives. Ops in ``hang`` are never
    answered (the proxy trickles bytes until the server gives up)."""

    def __init__(self, upstream: int, hang: set[str]) -> None:
        self.upstream = upstream
        self.hang = hang
        self.arrived: list[str] = []
        self._server = socket.create_server(("127.0.0.1", 0))
        self._server.settimeout(0.2)
        self._stop = threading.Event()
        self.port = self._server.getsockname()[1]
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self) -> None:
        while not self._stop.is_set():
            try:
                conn, _ = self._server.accept()
            except TimeoutError:
                continue
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

    def _handle(self, conn: socket.socket) -> None:
        with conn:
            line = conn.makefile("rb").readline()
            if not line:
                return
            op = json.loads(line).get("op")
            self.arrived.append(op)
            if op in self.hang:
                try:
                    while not self._stop.is_set():
                        conn.sendall(b"\x00")
                        time.sleep(0.1)
                except OSError:
                    return
                return
            with socket.create_connection(("127.0.0.1", self.upstream), timeout=5) as up:
                up.sendall(line)
                conn.sendall(up.makefile("rb").readline())

    def close(self) -> None:
        self._stop.set()
        self._thread.join(5)
        self._server.close()


@pytest.fixture
def proxy() -> Iterator[LoggingProxy]:
    with running_mock() as mock_port:
        logging_proxy = LoggingProxy(mock_port, hang={"write_pdo"})
        try:
            yield logging_proxy
        finally:
            logging_proxy.close()


class StdioClient:
    def __init__(self, tmp_path: Path, port: int) -> None:
        command, args = server_command()
        env = {
            **os.environ,
            "PYTHONUNBUFFERED": "1",
            "ETHERCAT_BACKEND": "mock",
            "ETHERCAT_MOCK_PORT": str(port),
            "ETHERCAT_TIMEOUT": "3000000",  # the hanging call holds the bridge for 3 s
            **GATES,
        }
        self.proc = subprocess.Popen(
            [command, *args],
            cwd=tmp_path,
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=open(tmp_path / "server-stderr.log", "w"),  # noqa: SIM115 - closed with the process
            text=True,
        )
        self.stderr_path = tmp_path / "server-stderr.log"
        self.replies: dict[int, dict] = {}
        threading.Thread(target=self._read, daemon=True).start()
        self.send(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "t", "version": "0"},
                },
            }
        )
        self.wait(1)
        self.send({"jsonrpc": "2.0", "method": "notifications/initialized"})

    def _read(self) -> None:
        for line in self.proc.stdout:
            message = json.loads(line)
            if "id" in message:
                self.replies[message["id"]] = message

    def send(self, message: dict) -> None:
        self.proc.stdin.write(json.dumps(message) + "\n")
        self.proc.stdin.flush()

    def call(self, request_id: int, name: str, arguments: dict) -> None:
        self.send(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "method": "tools/call",
                "params": {"name": name, "arguments": arguments},
            }
        )

    def wait(self, request_id: int, timeout: float = 30) -> dict | None:
        deadline = time.monotonic() + timeout
        while request_id not in self.replies and time.monotonic() < deadline:
            time.sleep(0.05)
        return self.replies.get(request_id)

    def close(self) -> None:
        if self.proc.poll() is None:
            self.proc.kill()
            self.proc.wait()


def bring_up(client: StdioClient) -> None:
    client.call(2, "scan_network", {})
    assert client.wait(2) is not None
    for request_id, state in ((3, "SAFEOP"), (4, "OP")):
        client.call(request_id, "set_slave_state", {"slave_position": 2, "state": state})
        assert client.wait(request_id) is not None


VICTIMS = {
    "write_sdo": {"slave_position": 3, "index": "0x8000", "subindex": 6, "value": 1, "data_type": "UINT8"},
    "set_slave_state": {"slave_position": 1, "state": "SAFEOP"},
}


@pytest.mark.parametrize("victim", sorted(VICTIMS))
def test_cancelling_a_queued_call_over_stdio_never_sends_it(tmp_path: Path, proxy: LoggingProxy, victim: str) -> None:
    client = StdioClient(tmp_path, proxy.port)
    try:
        bring_up(client)
        arrived_before = proxy.arrived.count(victim if victim != "set_slave_state" else "set_state")
        client.call(10, "write_pdo", {"slave_position": 2, "offset": 0, "data": [0x11]})  # hangs 3 s
        time.sleep(0.5)
        client.call(11, victim, VICTIMS[victim])  # queued behind it
        time.sleep(0.5)
        client.send({"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": 11}})
        assert client.wait(10, 30) is not None  # the hanging call ends, the lock frees
        time.sleep(1.0)
        op = victim if victim != "set_slave_state" else "set_state"
        assert proxy.arrived.count(op) == arrived_before, proxy.arrived
    finally:
        client.close()


def test_closing_stdin_never_sends_queued_writes(tmp_path: Path, proxy: LoggingProxy) -> None:
    client = StdioClient(tmp_path, proxy.port)
    try:
        bring_up(client)
        client.call(10, "write_pdo", {"slave_position": 2, "offset": 0, "data": [0x11]})  # hangs 3 s
        time.sleep(0.5)
        client.call(11, "write_sdo", VICTIMS["write_sdo"])  # queued behind it
        time.sleep(0.5)
        client.proc.stdin.close()  # the client disconnects
        code = client.proc.wait(timeout=30)  # in-flight handlers finish, then the server exits
        stderr = client.stderr_path.read_text()
        assert code == 0 and "Traceback" not in stderr, stderr[-3000:]
        time.sleep(0.5)
        assert "write_sdo" not in proxy.arrived, proxy.arrived
    finally:
        client.close()


# -- the last check right before each send ------------------------------------------------


def test_call_token_decides_atomically() -> None:
    from ethercat_mcp.ec_master import CallToken

    token = CallToken()
    token.begin_send()
    assert token.cancel("cancelled") is True  # it had sent: the caller reports "unknown"
    with pytest.raises(EthercatMasterError) as info:
        token.begin_send()
    assert info.value.outcome == "unknown"
    fresh = CallToken()
    assert fresh.cancel("cancelled") is False  # nothing sent: "not_sent", and it never will
    with pytest.raises(EthercatMasterError, match="will not be sent later") as info:
        fresh.begin_send()
    assert info.value.outcome == "not_sent"
    expired = CallToken(deadline_s=0.0)
    with pytest.raises(EthercatMasterError, match="deadline"):
        expired.begin_send()


def cancel_once_locked(backend: PysoemBackend, token) -> None:
    """The client cancels just after the call got the lock, before it sends."""
    original = backend._require_slave

    def require_then_cancel(position: int):
        slave = original(position)
        token.cancel("the call was cancelled")
        return slave

    backend._require_slave = require_then_cancel


@pytest.mark.parametrize("operation", ["write_pdo", "write_sdo", "set_state"])
def test_cancellation_between_lock_and_send_sends_nothing(operation: str) -> None:
    from ethercat_mcp.ec_master import CallToken

    _, backend, fake = master_with_fake()
    try:
        token = CallToken()
        cancel_once_locked(backend, token)
        calls_before = len(fake.bus.calls)
        with pytest.raises(EthercatMasterError, match="will not be sent later") as info:
            if operation == "write_pdo":
                backend.write_pdo(2, 0, b"\xa5", token=token)
            elif operation == "write_sdo":
                backend.write_sdo(3, 0x8000, 6, b"\x01", token=token)
            else:
                backend.set_state(1, SAFEOP_STATE, token=token)
        assert info.value.outcome == "not_sent"
        assert backend._slaves[2].output == b"\x00"
        assert fake.device(3).objects[(0x8000, 6)][0] == b"\x00"
        assert not any(call[0] == "slave.write_state" for call in fake.bus.calls[calls_before:])
    finally:
        backend.close()


def test_cancellation_before_config_init_does_not_reset_the_bus() -> None:
    from ethercat_mcp.ec_master import CallToken

    config = EthercatConfig.from_env({"ETHERCAT_INTERFACE": "fake0"})
    fake = FakePysoem()
    backend = PysoemBackend(config, pysoem_module=fake)
    token = CallToken()
    original = backend._ensure_open

    def open_then_cancel():
        master = original()
        token.cancel("the call was cancelled")
        return master

    backend._ensure_open = open_then_cancel
    try:
        with pytest.raises(EthercatMasterError, match="will not be sent later"):
            backend.scan(token=token)
        assert not any(call[0] == "config_init" for call in fake.bus.calls)
    finally:
        backend.close()


def test_mock_bridge_checks_the_token_after_connecting(proxy: LoggingProxy, monkeypatch: pytest.MonkeyPatch) -> None:
    from ethercat_mcp import mock_bridge
    from ethercat_mcp.ec_master import CallToken
    from ethercat_mcp.mock_bridge import MockBridgeBackend

    backend = MockBridgeBackend(
        EthercatConfig.from_env({"ETHERCAT_BACKEND": "mock", "ETHERCAT_MOCK_PORT": str(proxy.port)})
    )
    backend.scan()
    token = CallToken()
    connect = socket.create_connection

    def connect_then_cancel(*args, **kwargs):
        sock = connect(*args, **kwargs)
        token.cancel("the call was cancelled")  # cancelled while connecting
        return sock

    monkeypatch.setattr(mock_bridge.socket, "create_connection", connect_then_cancel)
    with pytest.raises(EthercatMasterError, match="will not be sent later") as info:
        backend.write_sdo(3, 0x8000, 6, b"\x01", token=token)
    assert info.value.outcome == "not_sent"
    time.sleep(0.3)
    assert "write_sdo" not in proxy.arrived
