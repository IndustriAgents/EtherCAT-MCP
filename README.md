# EtherCAT MCP Server

[![CI](https://github.com/IndustriAgents/EtherCAT-MCP/actions/workflows/ci.yml/badge.svg)](https://github.com/IndustriAgents/EtherCAT-MCP/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![MCP](https://img.shields.io/badge/MCP-compatible-purple)](https://modelcontextprotocol.io)
[![Part of IndustriConnect](https://img.shields.io/badge/part_of-IndustriConnect-0b7285)](https://github.com/IndustriAgents/IndustriConnect)

Model Context Protocol tooling for Beckhoff EtherCAT networks. It provides a
Python MCP server, an EtherCAT master built on
[PySOEM](https://pysoem.readthedocs.io/), that gives AI agents and other MCP
clients tools to discover slaves, read/write PDOs/SDOs, and inspect the master.
A mock slave is included for local experiments. The layout matches the other
[IndustriConnect](https://github.com/IndustriAgents/IndustriConnect) protocol
servers (Modbus, S7comm, ...), and tools return the suite's
`{ success, data, error, meta }` envelope (one exception is listed under
[known limitations](#known-limitations)).

> **Status: scaffolding.** Bus scan and slave info go through PySOEM. PDO/SDO
> access and state changes are still placeholders that do not touch the bus.
> The [Tools](#tools) table lists what each tool really does today.

## Layout

```
EtherCAT-MCP/                  # mirrored into IndustriConnect as EtherCAT-Project/
├── README.md
├── LICENSE, CONTRIBUTING.md, CODE_OF_CONDUCT.md, SECURITY.md
├── .github/                   # CI, issue/PR templates, IndustriConnect notification
├── ethercat-python/           # Python MCP server (uv, PySOEM)
└── ethercat-mock-slave/       # Mock EtherCAT slave (TCP/JSON) for tests
```

## Install

Requirements: Python 3.11+ and [uv](https://docs.astral.sh/uv/). To reach a
real bus you also need a network interface wired to the EtherCAT segment and
raw-socket privileges: root, or on Linux the `CAP_NET_RAW` and `CAP_NET_ADMIN`
capabilities (see [Quick start](#quick-start)). Where PySOEM publishes no wheel
(for example Apple silicon or Linux on ARM), `uv sync` builds it from source,
which needs a C toolchain.

```bash
git clone https://github.com/IndustriAgents/EtherCAT-MCP.git
cd EtherCAT-MCP/ethercat-python
uv sync
```

## Quick start

The server speaks MCP over stdio. Every command in this section runs from
`ethercat-python/`, where [Install](#install) left you.

### Run it as root

```bash
sudo ETHERCAT_INTERFACE=eth0 PYTHONDONTWRITEBYTECODE=1 .venv/bin/ethercat-mcp
```

This starts the entry point that `uv sync` installed into `.venv`, not
`sudo uv run ...`. Ubuntu, Debian, Fedora and RHEL give sudo a `secure_path`
that leaves out `~/.local/bin`, where uv's installer and pipx put `uv`, so
`sudo uv run` stops with `sudo: uv: command not found`.
`PYTHONDONTWRITEBYTECODE=1` keeps root from leaving root-owned `__pycache__`
folders in `.venv` and `src/`, which would make your next `uv sync` fail with
`Permission denied`.

`eth0` is a Linux interface name. macOS names them `en0`, `en1`, and so on.
To list the names PySOEM accepts on your machine (no root needed):

```bash
.venv/bin/python -c "import pysoem; print([a.name for a in pysoem.find_adapters()])"
```

### Let an MCP client start it

An MCP client starts the server as your user, without sudo. On Linux, grant the
virtual environment's interpreter the two capabilities SOEM needs, once: a raw
socket (`CAP_NET_RAW`) and switching the interface to promiscuous mode
(`CAP_NET_ADMIN`).

```bash
sudo setcap cap_net_raw,cap_net_admin+ep "$(readlink -f .venv/bin/python)"
```

The capabilities belong to the interpreter file that `readlink -f` resolves to,
not to this project. Every script that interpreter runs, in any virtual
environment built on it, gets them too. If it resolves to a system Python such
as `/usr/bin/python3.11`, that means every script run with that Python. Read
[Running it safely](SECURITY.md#running-it-safely) before you do this. A
reinstalled or upgraded interpreter starts without them, and
`sudo setcap -r "$(readlink -f .venv/bin/python)"` removes them.

Then add the server to your client, for example in the `mcpServers` block that
Claude Desktop's `claude_desktop_config.json` and most other clients use:

```json
{
  "mcpServers": {
    "EtherCAT": {
      "command": "uv",
      "args": ["--directory", "/absolute/path/to/EtherCAT-MCP/ethercat-python", "run", "ethercat-mcp"],
      "env": {
        "ETHERCAT_INTERFACE": "eth0",
        "ETHERCAT_WRITES_ENABLED": "false"
      }
    }
  }
}
```

macOS has no per-file capabilities. PySOEM opens interfaces there through
libpcap, and the BPF devices (`/dev/bpf*`) are root-only by default. A server
that a client starts as your user can still use the tools that do not touch the
bus, but it cannot open an interface.

The server opens the interface lazily, on the first tool that needs the bus.
Without hardware or privileges it still starts. `ping`, `get_master_status`,
`list_slaves` and `load_esi_file` work, and bus tools return
`success: false` with the reason (for example
`Failed to open interface eth0: could not open interface eth0`).

## Configuration

Set these in the server's environment (for example the `env` block above):

| Variable | Default | Meaning |
|----------|---------|---------|
| `ETHERCAT_INTERFACE` | `eth0` | Network interface PySOEM opens (`eth0` is a Linux name; macOS uses `en0`, `en1`, ...) |
| `ETHERCAT_CYCLE_TIME` | `1000` | Cycle time in µs (reported by `get_master_status`; there is no cyclic loop yet) |
| `ETHERCAT_TIMEOUT` | `500000` | Timeout in µs (reported by `get_master_status`) |
| `ETHERCAT_WRITES_ENABLED` | `true` | Gates `write_pdo`, `write_sdo`, `write_slave_by_alias` |
| `ETHERCAT_STATE_CHANGE_ENABLED` | `false` | Gates `set_slave_state` |
| `SLAVE_MAP_FILE` | unset | JSON file of slave aliases (see below) |
| `ETHERCAT_ESI_PATH` | unset | Directory searched for ESI files passed to `load_esi_file` by name |

`ETHERCAT_EXPECTED_WKC`, `ETHERCAT_MAX_RETRIES` and
`ETHERCAT_RETRY_BACKOFF_BASE` are parsed but not used yet.

Slave map format (`SLAVE_MAP_FILE`):

```json
{
  "coupler": { "position": 0, "offset": 0, "description": "EK1100 coupler", "vendor_id": "0x00000002" }
}
```

## Tools

| Tool | What it does today |
|------|--------------------|
| `ping` | Reports configuration, write/state gates and number of slave aliases. No bus access. |
| `get_master_status` | Interface, cycle time, timeout, gates, and slaves found by the last scan. No bus access. |
| `scan_network` | Opens the interface and runs PySOEM `config_init`. Returns position, name, state, vendor ID, product code, revision and I/O sizes per slave. |
| `get_slave_info` | Rescans and returns one slave's entry. |
| `read_pdo` | Opens the interface, then returns zero bytes of the requested length (placeholder: process data is not read yet). |
| `write_pdo` | Gated by `ETHERCAT_WRITES_ENABLED`. Opens the interface, then reports success without writing (placeholder). |
| `read_sdo` | Opens the interface, then returns value `0` (placeholder: CoE is not read yet). |
| `write_sdo` | Gated by `ETHERCAT_WRITES_ENABLED`. Opens the interface, then reports success without writing (placeholder). |
| `set_slave_state` | Gated by `ETHERCAT_STATE_CHANGE_ENABLED` (`INIT`/`PREOP`/`SAFEOP`/`OP`). Opens the interface, then reports success without changing the state (placeholder). |
| `test_slave_communication` | Opens the interface, then returns 4 zero bytes (the `read_pdo` placeholder). |
| `list_slaves` | Lists aliases from `SLAVE_MAP_FILE`. |
| `read_slave_by_alias` / `write_slave_by_alias` | Resolve an alias to position and offset, then call `read_pdo` / `write_pdo` (same placeholders, same write gate). |
| `load_esi_file` | Parses an ESI XML file into metadata, object dictionary and PDO lists. See [known limitations](#known-limitations). |

Planned but not implemented yet: real cyclic PDO exchange and CoE SDO access,
slave state control, working counter inspection and other diagnostics,
distributed clocks, FoE, and ESI-driven configuration guardrails.

## Mock slave

`ethercat-mock-slave/` is a minimal slave simulator. Instead of speaking raw
EtherCAT, it exposes a newline-delimited JSON API over TCP (default
`127.0.0.1:6700`) with `scan`, `read_pdo` and `write_pdo` operations over one
simulated EK1100 with changing input bytes.

From the repository root:

```bash
cd ethercat-mock-slave
uv sync
uv run ethercat-mock-slave            # foreground; --host, --port, --update-interval, --verbose
```

Or run it in Docker with `./run-container.sh 6700` from the same folder. The
script publishes the port on `127.0.0.1` only.

The MCP server does **not** connect to the mock today. The server drives a
network interface through PySOEM, and the mock answers JSON over TCP. The mock
is the starting point for a SOEM-based simulator. See
[ethercat-mock-slave/README.md](ethercat-mock-slave/README.md).

## Known limitations

- PDO/SDO reads and writes and slave state changes are placeholders (see
  [Tools](#tools)). A write that returns `success: true` has not written
  anything.
- `ETHERCAT_WRITES_ENABLED` defaults to `true`. Set it to `false` for a
  read-only server.
- `read_sdo` and `write_sdo` parse `index` with Python's `int(index, 0)` before
  their error handling. An index that is not a number comes back as a bare MCP
  tool error, not the envelope (for example
  `Error executing tool read_sdo: invalid literal for int() with base 0: 'not-hex'`).
  Write the index in hex with a `0x` prefix (`"0x1018"`). Without the prefix,
  `"1018"` is read as decimal.
- `load_esi_file` reads `Index`, `Name`, `BitLen` and similar values as XML
  attributes, but ETG ESI files carry them as child elements, so for real ESI
  files most fields come back `null`.
- A `.env` file is loaded at startup, but the master settings
  (`ETHERCAT_INTERFACE`, `ETHERCAT_CYCLE_TIME`, `ETHERCAT_TIMEOUT`, ...) are
  read before it is loaded. Set them in the real environment.
- The server and the mock do not talk to each other (see [Mock slave](#mock-slave)).

## Development

See [CONTRIBUTING.md](CONTRIBUTING.md) for setup, the checks CI runs, and how
changes flow into IndustriConnect.
[ethercat-python/README.md](ethercat-python/README.md) describes the server
package.

## Part of IndustriConnect

This server is one of the protocol servers in
[**IndustriConnect**](https://github.com/IndustriAgents/IndustriConnect), a
suite of MCP servers for industrial protocols (Modbus, MQTT/Sparkplug B, OPC UA,
BACnet, DNP3, EtherCAT, EtherNet/IP, PROFIBUS, PROFINET and S7comm) that share
one layout and one response envelope. IndustriConnect includes this repository
as the `EtherCAT-Project` git submodule, which tracks `main`. Issues and pull
requests for the EtherCAT server belong here.

This repository was split out of IndustriConnect's `EtherCAT-Project/` folder
with its git history. Pull request numbers in commit messages from before the
split (#5, #16, #18, #19) refer to
[IndustriConnect pull requests](https://github.com/IndustriAgents/IndustriConnect/pulls),
not to this repository.

## Security

EtherCAT has no authentication, and this server needs raw-socket access to a
control network. Read [SECURITY.md](SECURITY.md) before pointing it at real
hardware, and use it to report vulnerabilities privately.

## License

MIT — see [LICENSE](LICENSE).
