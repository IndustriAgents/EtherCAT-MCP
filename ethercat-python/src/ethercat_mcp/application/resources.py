from __future__ import annotations

from dataclasses import dataclass

from ethercat_mcp.config import EthercatConfig
from ethercat_mcp.domain.ports import EthercatMasterPort
from ethercat_mcp.esi_parser import ESIParser
from ethercat_mcp.infrastructure.maps import SlaveMap


@dataclass(slots=True)
class ToolResources:
    master: EthercatMasterPort
    config: EthercatConfig
    slave_map: SlaveMap
    esi_parser: ESIParser
