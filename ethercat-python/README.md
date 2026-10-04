# ethercat-python

Python MCP server for EtherCAT using [PySOEM](https://pysoem.readthedocs.io/). It exposes the `ethercat-mcp` CLI so MCP-compatible clients can scan the bus, read slave states, read and write CoE objects (SDOs), exchange process data (PDOs), request state changes and parse ESI files. Writes and state changes are off by default. Every tool returns a `{ success, data, error, meta }` envelope, invalid arguments included, and `meta.backend` says whether a real bus (`pysoem`) or the mock slave (`mock`) answered. The PySOEM paths are unit-tested against a fake `pysoem` and have not been verified on hardware yet; see [Known limitations](../README.md#known-limitations).

## Requirements

- Python 3.11+
- [uv](https://github.com/astral-sh/uv)
- For a real bus: a network interface wired to the EtherCAT segment, and root (or `cap_net_raw` and `cap_net_admin` on Linux)
- A C toolchain where PySOEM has no wheel (Apple silicon, Linux on ARM); on Windows, [Npcap](https://npcap.com/)

## Quick Start

Run these from this folder (`ethercat-python/`).

Against the mock slave (start it first, see [`../ethercat-mock-slave`](../ethercat-mock-slave)):

```bash
uv sync
npx @modelcontextprotocol/inspector -e ETHERCAT_BACKEND=mock uv run ethercat-mcp
```

Against a real bus, as root:

```bash
uv sync
sudo ETHERCAT_INTERFACE=eth0 PYTHONDONTWRITEBYTECODE=1 .venv/bin/ethercat-mcp
```

sudo resets the environment, so variables you exported and an MCP client's `env` block do not reach the root process. Put every setting on the sudo line as above, or use `--env-file PATH` (or a `.env` in the current directory). `--print-config` shows what the server sees, and the process then waits on stdin for an MCP client (for example `npx @modelcontextprotocol/inspector sudo ETHERCAT_INTERFACE=eth0 PYTHONDONTWRITEBYTECODE=1 .venv/bin/ethercat-mcp`). The server runs from `.venv` because `sudo uv run` usually stops with `sudo: uv: command not found`: sudo's `secure_path` (Ubuntu, Debian, Fedora, RHEL) leaves out `~/.local/bin`, where uv's installer and pipx put `uv`. `PYTHONDONTWRITEBYTECODE=1` keeps root from leaving root-owned `__pycache__` folders that would break your next `uv sync`. `eth0` is a Linux example; macOS names interfaces `en0`, `en1`, and so on. To let an MCP client start the server without sudo on Linux (`setcap` on the interpreter), see [Real bus](../README.md#real-bus) in the top-level README.

[Configuration](../README.md#configuration) in the top-level README lists every setting. All of them are read at startup, after `.env` (from the current directory only) is loaded; variables already in the environment win.

## Layout

```
ethercat-python/
├── README.md
├── pyproject.toml
├── uv.lock
├── .python-version
├── src/ethercat_mcp
│   ├── cli.py           # argparse, .env loading, startup
│   ├── config.py        # EthercatConfig.from_env()
│   ├── server.py        # FastMCP wiring, envelope for bad arguments, stdout guard
│   ├── tools.py         # the 14 MCP tools, slave map
│   ├── ec_master.py     # PySOEM backend, cyclic process data, async facade with the gates
│   ├── mock_bridge.py   # mock transport (JSON over TCP to ethercat-mock-slave)
│   ├── codec.py         # argument parsing, CoE value encoding
│   └── esi_parser.py    # ETG.2000 ESI parser
└── tests/
    ├── fake_pysoem.py   # stand-in for the PySOEM API used by the backend
    ├── fixtures/        # hand-written, ESI-shaped sample (not a vendor file)
    └── test_*.py        # unit tests, stdio tests, end-to-end test against the mock
```

## Tests

```bash
uv sync
uv run pytest
uv run ruff check . && uv run ruff format --check .
```

`tests/test_integration_mock.py` starts `../ethercat-mock-slave/ethercat_mock_slave.py` on a free loopback port and drives `ethercat-mcp` over stdio with the MCP client SDK. Nothing in the suite opens a network interface.
