"""Compatibility imports; implementation lives in domain, application and protocol modules."""

import json as json
from dataclasses import dataclass as dataclass
from functools import partial as partial
from pathlib import Path as Path
from typing import Any as Any

import anyio as anyio
from pydantic import StrictInt as StrictInt

from ethercat_mcp._version import __version__ as __version__
from ethercat_mcp.application.resources import ToolResources as ToolResources
from ethercat_mcp.codec import decode_value as decode_value
from ethercat_mcp.codec import encode_value as encode_value
from ethercat_mcp.codec import parse_bytes as parse_bytes
from ethercat_mcp.codec import parse_int as parse_int
from ethercat_mcp.config import EthercatConfig as EthercatConfig
from ethercat_mcp.domain.errors import NOT_SENT as NOT_SENT
from ethercat_mcp.domain.errors import WRITTEN as WRITTEN
from ethercat_mcp.domain.errors import parse_state as parse_state
from ethercat_mcp.domain.model import ESI_DEADLINE_S as ESI_DEADLINE_S
from ethercat_mcp.domain.model import MAX_PDO_BYTES as MAX_PDO_BYTES
from ethercat_mcp.domain.model import envelope as envelope
from ethercat_mcp.ec_master import EthercatMaster as EthercatMaster
from ethercat_mcp.esi_parser import ESIParser as ESIParser
from ethercat_mcp.infrastructure.maps import SlaveMap as SlaveMap

from .protocol.support import READ_ONLY as READ_ONLY
from .protocol.support import SCAN as SCAN
from .protocol.support import WRITES as WRITES
from .protocol.support import FastMCP as FastMCP
from .protocol.support import ToolAnnotations as ToolAnnotations
from .protocol.tools import register_tools as register_tools
