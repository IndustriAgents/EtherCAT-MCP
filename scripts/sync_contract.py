"""Regenerate advertised output schemas and the packaged canonical contract.

Edit contract/tools.json (including resultShapes) first, then run this script.
Verification remains read-only in verify_contract.py.
"""

import copy
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
path = ROOT / "contract/tools.json"
contract = json.loads(path.read_text())
for tool in contract["tools"]:
    shape = contract["resultShapes"][contract["routes"][tool["name"]]["resultShape"]]
    tool["outputSchema"]["properties"]["data"] = {
        "anyOf": [{"type": "null"}, copy.deepcopy(shape)]
    }
bundled, = ROOT.glob("*-python/src/*/_contract/tools.json")
serialized = json.dumps(contract, indent=2, ensure_ascii=False) + "\n"
path.write_text(serialized)
bundled.write_text(serialized)
