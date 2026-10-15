"""Compatibility import; implementation lives in adapters.ec_master."""

import sys

from .adapters import ec_master as _implementation

sys.modules[__name__] = _implementation
