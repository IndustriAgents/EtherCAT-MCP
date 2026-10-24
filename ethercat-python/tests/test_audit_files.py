"""Audit configuration must refuse non-files before dispatch or stream output."""

import os
import subprocess
import sys

import pytest

from ethercat_mcp.application.execution import Execution
from ethercat_mcp.domain.policy import Policy


def test_device_file_is_refused_before_writing(monkeypatch):
    writes = []
    monkeypatch.setattr(os, "write", lambda fd, data: writes.append(bytes(data)) or len(data))
    execution = Execution(Policy(audit_path=os.devnull))
    with pytest.raises(OSError, match="regular file"):
        execution.admit("write", "write", write=True, local=False)
    assert writes == []
    assert execution.active == execution.controls == 0


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="FIFOs require POSIX")
def test_fifo_with_reader_is_refused_without_putting_data_on_stream(tmp_path):
    path = tmp_path / "audit.pipe"
    os.mkfifo(path)
    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
    try:
        execution = Execution(Policy(audit_path=str(path)))
        with pytest.raises(OSError, match="regular file"):
            execution.admit("write", "write", write=True, local=False)
        assert os.read(fd, 4096) == b""
        assert execution.active == execution.controls == 0
    finally:
        os.close(fd)


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="FIFOs require POSIX")
def test_fifo_without_reader_cannot_hang_admission(tmp_path):
    path = tmp_path / "audit.pipe"
    os.mkfifo(path)
    code = """
import sys
from ethercat_mcp.application.execution import Execution
from ethercat_mcp.domain.policy import Policy
execution = Execution(Policy(audit_path=sys.argv[1]))
try:
    execution.admit("write", "write", write=True, local=False)
except OSError:
    assert execution.active == execution.controls == 0
else:
    raise AssertionError("FIFO audit unexpectedly admitted a write")
"""
    result = subprocess.run(
        [sys.executable, "-c", code, str(path)],
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert result.returncode == 0, result.stderr
