# ethercat-python

Python MCP server for EtherCAT using [PySOEM](https://pysoem.readthedocs.io/). It exposes the `ethercat-mcp` CLI so MCP-compatible clients can scan slaves, read/write PDOs and SDOs, and inspect the master. Tools return structured `{ success, data, error, meta }` payloads, with one exception listed under [known limitations](../README.md#known-limitations). PDO/SDO access and state changes are still placeholders. The [Tools](../README.md#tools) table in the top-level README lists what each tool does today.

## Requirements

- Python 3.11+
- [uv](https://github.com/astral-sh/uv)
- Network interface with EtherCAT access (root, or `cap_net_raw` and `cap_net_admin` on Linux)
- PySOEM prerequisites (SOEM compiled, lib installed if needed)

## Quick Start

Run these from this folder (`ethercat-python/`):

```bash
uv sync
sudo ETHERCAT_INTERFACE=eth0 PYTHONDONTWRITEBYTECODE=1 .venv/bin/ethercat-mcp
```

The server runs from `.venv` because `sudo uv run` usually stops with `sudo: uv: command not found`: sudo's `secure_path` (Ubuntu, Debian, Fedora, RHEL) leaves out `~/.local/bin`, where uv's installer and pipx put `uv`. `PYTHONDONTWRITEBYTECODE=1` keeps root from leaving root-owned `__pycache__` folders that would break your next `uv sync`. `eth0` is a Linux example; macOS names interfaces `en0`, `en1`, and so on. To let an MCP client start the server without sudo on Linux (`setcap` on the interpreter), see [Quick start](../README.md#quick-start) in the top-level README.

Set environment variables for `ETHERCAT_INTERFACE`, cycle time, write/state toggles, `SLAVE_MAP_FILE`, `ETHERCAT_ESI_PATH`, etc. See [Configuration](../README.md#configuration) in the top-level README for the full list. A `.env` file is also loaded at startup, but it currently does not reach the master settings (`ETHERCAT_INTERFACE`, cycle time, timeout), as described under [known limitations](../README.md#known-limitations).

## Layout

```
ethercat-python/
├── README.md
├── pyproject.toml
├── uv.lock
├── .python-version
└── src/ethercat_mcp
    ├── __init__.py
    ├── cli.py
    ├── server.py
    ├── tools.py
    ├── ec_master.py
    └── esi_parser.py
```

The current implementation wires a master wrapper (`ec_master.py`), ESI parser, device/slave map logic, and core MCP tools (scan network, slave info, PDO/SDO read/write stubs, alias helpers, ping). Extend these modules to add the planned features listed in the top-level README (cyclic process data, distributed clocks, advanced diagnostics, FoE, etc.).
