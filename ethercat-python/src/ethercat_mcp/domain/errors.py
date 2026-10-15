from __future__ import annotations

INIT_STATE = 1
PREOP_STATE = 2
SAFEOP_STATE = 4
OP_STATE = 8
SETTABLE_STATES = {"INIT": INIT_STATE, "PREOP": PREOP_STATE, "SAFEOP": SAFEOP_STATE, "OP": OP_STATE}
_STATE_ALIASES = {
    "PRE-OP": "PREOP",
    "PRE_OP": "PREOP",
    "PREOPERATIONAL": "PREOP",
    "PRE-OPERATIONAL": "PREOP",
    "SAFE-OP": "SAFEOP",
    "SAFE_OP": "SAFEOP",
    "SAFEOPERATIONAL": "SAFEOP",
    "SAFE-OPERATIONAL": "SAFEOP",
    "OPERATIONAL": "OP",
}


def parse_state(state: str) -> int:
    """Map ``INIT``/``PREOP``/``SAFEOP``/``OP`` (and common spellings) to a state code."""
    key = state.strip().upper() if isinstance(state, str) else ""
    key = _STATE_ALIASES.get(key, key)
    if key not in SETTABLE_STATES:
        raise ValueError(f"Invalid state {state!r} (expected one of INIT, PREOP, SAFEOP, OP)")
    return SETTABLE_STATES[key]


WRITTEN = "written"
NOT_SENT = "not_sent"
