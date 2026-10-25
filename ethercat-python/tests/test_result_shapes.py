"""The advertised result contract must describe data, including failure envelopes."""

import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator, ValidationError

CONTRACT = json.loads((Path(__file__).resolve().parents[2] / "contract/tools.json").read_text())


@pytest.mark.parametrize("tool", CONTRACT["tools"], ids=lambda tool: tool["name"])
def test_every_tool_has_a_structural_result_contract(tool):
    shape = CONTRACT["resultShapes"][CONTRACT["routes"][tool["name"]]["resultShape"]]
    assert any(key in shape for key in ("type", "anyOf", "oneOf"))
    assert tool["outputSchema"]["properties"]["data"] == {"anyOf": [{"type": "null"}, shape]}
    Draft202012Validator.check_schema(tool["outputSchema"])
    validator = Draft202012Validator(tool["outputSchema"])
    # Every tool must admit a failure with no data, even after possible dispatch.
    refusal = {"success": False, "data": None, "error": "refused", "meta": {"outcome": "unknown", "request_sent": None}}
    validator.validate(refusal)
    # A scalar cannot silently stand in for a structured protocol result.
    with pytest.raises(ValidationError):
        validator.validate({**refusal, "data": 42})
