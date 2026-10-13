"""Suite rule 11: a mock started by the tests does not outlive a killed test run."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time

import pytest
from conftest import MOCK_SCRIPT

PARENT = """
import subprocess, sys, time
mock = subprocess.Popen(
    [sys.executable, sys.argv[1], "--host", "127.0.0.1", "--port", "0", "--exit-on-stdin-eof"],
    stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
)
print(mock.pid, flush=True)
time.sleep(120)
"""


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    try:  # an exited but not yet reaped process (zombie) counts as gone
        with open(f"/proc/{pid}/stat") as stat:
            return stat.read().rsplit(")", 1)[1].split()[0] != "Z"
    except OSError:
        return True


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX signals")
def test_mock_exits_when_the_test_process_is_killed() -> None:
    parent = subprocess.Popen([sys.executable, "-c", PARENT, str(MOCK_SCRIPT)], stdout=subprocess.PIPE, text=True)
    mock_pid = int(parent.stdout.readline())
    try:
        assert alive(mock_pid)
        parent.send_signal(signal.SIGKILL)  # no cleanup code runs in the parent
        parent.wait(timeout=10)
        deadline = time.monotonic() + 15
        while alive(mock_pid) and time.monotonic() < deadline:
            time.sleep(0.1)
        assert not alive(mock_pid), "the mock outlived its killed parent"
    finally:
        if alive(mock_pid):
            os.kill(mock_pid, signal.SIGKILL)
        if parent.poll() is None:
            parent.kill()
            parent.wait()
