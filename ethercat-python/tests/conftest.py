from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

TESTS = Path(__file__).resolve().parent
FIXTURES = TESTS / "fixtures"
REPO = TESTS.parents[1]
MOCK_SCRIPT = REPO / "ethercat-mock-slave" / "ethercat_mock_slave.py"

sys.path.insert(0, str(TESTS))


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture(autouse=True)
def _clean_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tests must not pick up ETHERCAT_* settings from the developer's shell."""
    import os

    for key in list(os.environ):
        if key.startswith(("ETHERCAT_", "SLAVE_MAP_FILE", "MOCK_ETHERCAT_")):
            monkeypatch.delenv(key, raising=False)


def envelope_from(result: Any) -> dict[str, Any]:
    """Pull the envelope out of what FastMCP.call_tool returns (content, or (content, structured))."""
    if isinstance(result, tuple):
        content, structured = result
        if isinstance(structured, dict) and "success" in structured:
            return structured
        result = content
    if isinstance(result, dict):
        return result
    return json.loads(result[0].text)
