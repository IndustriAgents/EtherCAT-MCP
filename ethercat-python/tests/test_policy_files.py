"""Deployment policy loading remains bounded and fails closed."""

import json
import os
import stat
import subprocess
import sys
from types import SimpleNamespace

import pytest

from ethercat_mcp.application.execution import Execution
from ethercat_mcp.infrastructure.policy_file import read_policy_document


def test_policy_file_is_applied_at_construction(tmp_path, monkeypatch):
    path = tmp_path / "policy.json"
    path.write_text('{"read_only": true, "allowed_tools": []}')
    monkeypatch.setenv("RECHECK_POLICY_FILE", str(path))
    policy = Execution.from_environment("recheck").policy
    assert policy.read_only is True
    assert policy.allowed_tools == ()


def test_file_growth_after_stat_cannot_bypass_size_bound(tmp_path, monkeypatch):
    path = tmp_path / "policy.json"
    path.write_bytes(b" " * 65537)
    monkeypatch.setattr(os, "fstat", lambda fd: SimpleNamespace(st_mode=stat.S_IFREG, st_size=0))
    with pytest.raises(ValueError, match="64 KiB"):
        read_policy_document(path)


def test_duplicate_policy_fields_are_refused(tmp_path):
    path = tmp_path / "policy.json"
    path.write_text('{"read_only": true, "read_only": false}')
    with pytest.raises(ValueError, match="duplicate"):
        read_policy_document(path)


def test_deep_policy_is_a_configuration_error(tmp_path):
    path = tmp_path / "policy.json"
    path.write_text("[" * 2000 + "0" + "]" * 2000)
    with pytest.raises(ValueError, match="excessive nesting"):
        read_policy_document(path)


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="FIFOs require POSIX")
def test_policy_fifo_cannot_hang_server_construction(tmp_path):
    path = tmp_path / "policy.pipe"
    os.mkfifo(path)
    code = """
import sys
from ethercat_mcp.infrastructure.policy_file import read_policy_document
try:
    read_policy_document(sys.argv[1])
except ValueError as exc:
    assert "regular file" in str(exc)
else:
    raise AssertionError("FIFO was accepted as policy")
"""
    result = subprocess.run(
        [sys.executable, "-c", code, str(path)],
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert result.returncode == 0, result.stderr


def test_braces_inside_json_strings_do_not_count_as_nesting(tmp_path):
    path = tmp_path / "policy.json"
    document = {"audit_path": "[" * 100 + '"' + "]" * 100}
    path.write_text(json.dumps(document))
    assert read_policy_document(path) == document
