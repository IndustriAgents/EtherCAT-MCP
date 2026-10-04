# EtherCAT MCP Server

[![CI](https://github.com/IndustriAgents/EtherCAT-MCP/actions/workflows/ci.yml/badge.svg)](https://github.com/IndustriAgents/EtherCAT-MCP/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![MCP](https://img.shields.io/badge/MCP-compatible-purple)](https://modelcontextprotocol.io)
[![Part of IndustriConnect](https://img.shields.io/badge/part_of-IndustriConnect-0b7285)](https://github.com/IndustriAgents/IndustriConnect)

Model Context Protocol tooling for EtherCAT networks (any vendor's slaves; the
examples use Beckhoff EK1100/EL1008/EL2008/EL3102 terminals). It provides a
Python MCP server, an EtherCAT master built on
[PySOEM](https://pysoem.readthedocs.io/), that gives AI agents and other MCP
clients tools to scan the bus, read slave states, read and write CoE objects
(SDOs), exchange process data (PDOs) and request state changes, plus an ESI file
parser. Writes and state changes are **off by default**. A mock slave and a
matching mock transport let you exercise every tool without hardware. The
layout matches the other
[IndustriConnect](https://github.com/IndustriAgents/IndustriConnect) protocol
servers (Modbus, S7comm, ...), and every tool returns the suite's
`{ success, data, error, meta }` envelope, including for invalid arguments.

> **Status: early, not yet verified on hardware.** The PySOEM paths (scan, SDO
> read/write, process image and cyclic exchange, state changes) are written
> against PySOEM 1.1.12's API and unit-tested against a fake `pysoem` module,
> because CI has no EtherCAT hardware. End-to-end tests drive the real server
> over stdio against the mock slave. Try it on a bench before anything else,
> and read [Known limitations](#known-limitations).

## Layout

```
EtherCAT-MCP/                  # mirrored into IndustriConnect as EtherCAT-Project/
├── README.md
├── LICENSE, CONTRIBUTING.md, CODE_OF_CONDUCT.md, SECURITY.md
├── .github/                   # CI, issue/PR templates, IndustriConnect notification
├── ethercat-python/           # Python MCP server (uv, PySOEM) + tests/
└── ethercat-mock-slave/       # Mock EtherCAT slaves (TCP/JSON) + tests/
```

## Install

Requirements: Python 3.11+ and [uv](https://docs.astral.sh/uv/). PySOEM ships
SOEM inside its package, so nothing else has to be installed first, with two
exceptions:

- Where PySOEM publishes no wheel (for example Apple silicon or Linux on ARM),
  `uv sync` builds it from source, which needs a C toolchain.
- On Windows, PySOEM needs [Npcap](https://npcap.com/) (installed in
  WinPcap API-compatible mode).

To reach a real bus you also need a network interface wired to the EtherCAT
segment and raw-socket privileges: root, or on Linux the `CAP_NET_RAW` and
`CAP_NET_ADMIN` capabilities (see [Real bus](#real-bus)).

```bash
git clone https://github.com/IndustriAgents/EtherCAT-MCP.git
cd EtherCAT-MCP/ethercat-python
uv sync
```

## Quick start

The server speaks MCP over stdio: an MCP client starts it and talks to it over
stdin/stdout. Started by hand, it just waits for a client on stdin.
`ethercat-mcp --help` lists the options and settings.

### Without hardware: the mock

Start the mock slave in one terminal:

```bash
cd EtherCAT-MCP/ethercat-mock-slave
uv sync
uv run ethercat-mock-slave            # 127.0.0.1:6700
```

Then point the server at it with `ETHERCAT_BACKEND=mock`, for example from the
[MCP Inspector](https://modelcontextprotocol.io/docs/tools/inspector), run from
`ethercat-python/` (pass settings to the server with `-e`):

```bash
npx @modelcontextprotocol/inspector -e ETHERCAT_BACKEND=mock uv run ethercat-mcp
```

or from a client's `mcpServers` block:

```json
{
  "mcpServers": {
    "EtherCAT (mock)": {
      "command": "uv",
      "args": ["--directory", "/absolute/path/to/EtherCAT-MCP/ethercat-python", "run", "ethercat-mcp"],
      "env": {
        "ETHERCAT_BACKEND": "mock",
        "ETHERCAT_MOCK_PORT": "6700"
      }
    }
  }
}
```

Every answer from this backend carries `meta.backend: "mock"` and
`meta.simulated: true`, so it cannot be mistaken for a real bus. To try the
write path, add `"ETHERCAT_WRITES_ENABLED": "true"` and
`"ETHERCAT_STATE_CHANGE_ENABLED": "true"`, then: `scan_network`;
`set_slave_state` slave 2 (EL2008) to `SAFEOP`, then `OP`; `write_pdo` slave 2,
offset 0, data `[165]`; `set_slave_state` slave 1 (EL1008) to `SAFEOP`;
`read_pdo` slave 1 returns `a5`, because the mock wires the EL2008 outputs back
to the EL1008 inputs.

### Real bus

Read [SECURITY.md](SECURITY.md) and [Safety model](#safety-model) first.

#### Run it as root

```bash
sudo ETHERCAT_INTERFACE=eth0 PYTHONDONTWRITEBYTECODE=1 .venv/bin/ethercat-mcp
```

**sudo resets the environment.** Variables you exported in your shell, and an
MCP client's `env` block, do not reach a process started with sudo. Only what is
written on the sudo command line arrives (as above), so put every setting
there, or in a file the server reads itself:

```bash
sudo PYTHONDONTWRITEBYTECODE=1 .venv/bin/ethercat-mcp --env-file ethercat.env
```

A `.env` in the current directory works too, since sudo keeps the working
directory. `sudo --preserve-env=ETHERCAT_INTERFACE,ETHERCAT_WRITES_ENABLED ...`
also works where the sudoers policy allows it. To check what a root-run server
actually sees, add `--print-config`: it prints the effective settings as JSON
and exits.

The server then waits on stdin for an MCP client. To use it interactively, let
the client start the sudo command, for example the Inspector:

```bash
npx @modelcontextprotocol/inspector sudo ETHERCAT_INTERFACE=eth0 PYTHONDONTWRITEBYTECODE=1 .venv/bin/ethercat-mcp
```

If sudo needs a password, run `sudo -v` in the same terminal first.

This starts the entry point that `uv sync` installed into `.venv`, not
`sudo uv run ...`. Ubuntu, Debian, Fedora and RHEL give sudo a `secure_path`
that leaves out `~/.local/bin`, where uv's installer and pipx put `uv`, so
`sudo uv run` stops with `sudo: uv: command not found`.
`PYTHONDONTWRITEBYTECODE=1` keeps root from leaving root-owned `__pycache__`
folders in `.venv` and `src/`, which would make your next `uv sync` fail with
`Permission denied`.

`eth0` is a Linux interface name. macOS names them `en0`, `en1`, and so on, and
Windows uses `\Device\NPF_{...}` names. To list the names PySOEM accepts on
your machine (no root needed):

```bash
.venv/bin/python -c "import pysoem; print([a.name for a in pysoem.find_adapters()])"
```

#### Let an MCP client start it

An MCP client starts the server as your user, without sudo and without a
terminal to type a password into. On Linux, grant the virtual environment's
interpreter the two capabilities SOEM needs, once: a raw socket
(`CAP_NET_RAW`) and switching the interface to promiscuous mode
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

Then add the server to your client:

```json
{
  "mcpServers": {
    "EtherCAT": {
      "command": "uv",
      "args": ["--directory", "/absolute/path/to/EtherCAT-MCP/ethercat-python", "run", "ethercat-mcp"],
      "env": {
        "ETHERCAT_INTERFACE": "eth0"
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
`list_slaves` and `load_esi_file` work, and bus tools return `success: false`
with the reason (for example
`Failed to open interface eth0: could not open interface eth0`).

## Configuration

Set these in the server's environment (for example the `env` block above), or
in a `.env` file:

| Variable | Default | Meaning |
|----------|---------|---------|
| `ETHERCAT_BACKEND` | `pysoem` | `pysoem`: a real bus through PySOEM. `mock`: the mock slave over TCP/JSON (simulated). |
| `ETHERCAT_INTERFACE` | `eth0` | Network interface PySOEM opens (`pysoem` backend) |
| `ETHERCAT_CYCLE_TIME` | `1000` | Process-data cycle in µs (100 to 1,000,000). The exchange starts when a slave is first brought to SAFEOP or OP. Best effort: it is a Python thread, not a real-time task. |
| `ETHERCAT_TIMEOUT` | `500000` | µs to wait for a requested state in `set_slave_state`; also the timeout of each mock request |
| `ETHERCAT_EXPECTED_WKC` | `0` | If set, `write_pdo` fails when the working counter is below it. `0`: only a working counter above 0 is required. |
| `ETHERCAT_WRITES_ENABLED` | `false` | Gates `write_pdo`, `write_sdo`, `write_slave_by_alias` |
| `ETHERCAT_STATE_CHANGE_ENABLED` | `false` | Gates `set_slave_state` |
| `ETHERCAT_MOCK_HOST` | `127.0.0.1` | Mock slave address (`mock` backend) |
| `ETHERCAT_MOCK_PORT` | `6700` | Mock slave port (`mock` backend) |
| `SLAVE_MAP_FILE` | unset | JSON file of slave aliases (see below) |
| `ETHERCAT_ESI_PATH` | unset | Directory searched for ESI files passed to `load_esi_file` by name |

Booleans accept `true`/`false`, `1`/`0`, `yes`/`no` and `on`/`off`. A value
that cannot be used (for example `ETHERCAT_WRITES_ENABLED=maybe`) stops the
server at startup with a message on stderr.

All settings are read once at startup, after the `.env` file is loaded. The
server loads `.env` from the directory it is started in (with
`uv --directory .../ethercat-python run`, that is `ethercat-python/`), never
from parent directories. `--env-file PATH` loads another file instead.
Variables already set in the environment win over the file.

Slave map format (`SLAVE_MAP_FILE`). `position` is required, `offset` defaults
to 0, `length` is optional; numbers may be written as JSON numbers or as
strings such as `"0x10"`:

```json
{
  "inputs": { "position": 1, "offset": 0, "length": 1, "description": "EL1008 inputs" },
  "outputs": { "position": 2, "offset": 0, "description": "EL2008 outputs" }
}
```

## Safety model

- **Read-only by default.** `ETHERCAT_WRITES_ENABLED` and
  `ETHERCAT_STATE_CHANGE_ENABLED` default to `false`. With both off the server
  never moves a slave above PRE-OP and never writes outputs or CoE objects.
- **A scan is not passive.** `scan_network` runs SOEM's `config_init`, which
  resets every slave on the segment to INIT and then requests PRE-OP. That is
  how SOEM enumerates a bus. The server refuses to rescan while any slave is in
  SAFEOP or OP. Never connect the server to a segment another master is running.
- **Process data needs a state change.** The process image is mapped
  (`config_map`, with SOEM's automatic SAFE-OP request switched off) the first
  time `set_slave_state` asks for SAFEOP or OP. From then on a background thread
  exchanges process data every `ETHERCAT_CYCLE_TIME`. Inputs are valid in SAFEOP
  and OP; outputs are applied only in OP, so `write_pdo` refuses any other state.
- **Shutdown.** When the server exits after process data was running, it stops
  the exchange and requests INIT for all slaves before closing the interface.
- **The mock is labelled.** With `ETHERCAT_BACKEND=mock`, every envelope carries
  `meta.backend: "mock"` and `meta.simulated: true`, and the server logs a
  warning on startup.

## Tools

`meta.backend` in every envelope says who answered (`pysoem` or `mock`).

| Tool | What it does |
|------|--------------|
| `ping` | Server version, backend, gates, slave-map status. No bus access. |
| `get_master_status` | Interface or mock endpoint, gates, slaves from the last scan, process-image and cyclic-exchange state, last working counter. No bus access. |
| `scan_network` | Enumerates the bus (`config_init`, see [Safety model](#safety-model)). Returns position, name, vendor ID, product code, revision, AL state and process-data sizes per slave. The sizes are `null` on a real bus until the process image is mapped. |
| `get_slave_info` | One slave from the last scan, with its AL state read fresh. Needs a prior scan. |
| `test_slave_communication` | Reads the slave's AL state, then tries a CoE read of object 0x1000. `success: false` if the slave does not answer. |
| `read_pdo` | Input bytes of one slave from the process image (`offset`, `length`). Needs SAFEOP or OP. |
| `write_pdo` | Gated by `ETHERCAT_WRITES_ENABLED`. Writes output bytes into the process image and waits until they went out and the frame was answered. Needs OP. |
| `read_sdo` | CoE upload (`index`, `subindex`, optional `data_type` such as `UINT32`, `STRING`). Returns the raw bytes and the decoded value. Needs a mailbox and PREOP or higher. |
| `write_sdo` | Gated by `ETHERCAT_WRITES_ENABLED`. CoE download of `value` encoded as `data_type`. SDO aborts come back with their abort code. |
| `set_slave_state` | Gated by `ETHERCAT_STATE_CHANGE_ENABLED`. Requests `INIT`, `PREOP`, `SAFEOP` or `OP` for one slave and waits until it is reached; reports the AL status code if not. |
| `list_slaves` | Aliases from `SLAVE_MAP_FILE`, with an error per unusable entry. |
| `read_slave_by_alias` / `write_slave_by_alias` | Resolve an alias to position, offset and length, then do what `read_pdo` / `write_pdo` do (same rules, same write gate). |
| `load_esi_file` | Parses an ESI XML file: vendor, devices, and for one selected device (`device_type`, `revision`) its object dictionary and Tx/Rx PDO mappings. No bus access. |

Index arguments take hex with a `0x` prefix (`"0x1018"`), ESI-style `#x1018`,
or decimal (`"4120"`). Invalid arguments, including wrong types, come back as
`success: false` envelopes.

## Mock slave

`ethercat-mock-slave/` simulates a small segment behind a newline-delimited JSON
API over TCP (default `127.0.0.1:6700`): an EK1100 coupler, an EL1008 (8
digital inputs), an EL2008 (8 digital outputs, wired back to the EL1008 inputs)
and an EL3102 (2 analog inputs with CoE objects). It implements the AL state
machine, the process-data rules above and CoE with SDO abort codes. It uses
only the Python standard library. It is a simulator for testing, with
simplified values, not a conformance reference. See
[ethercat-mock-slave/README.md](ethercat-mock-slave/README.md) for the JSON
protocol.

```bash
cd ethercat-mock-slave
uv sync
uv run ethercat-mock-slave            # --host, --port (0 = any free port), --update-interval, --verbose
```

Or run it in Docker with `./run-container.sh 6700` from the same folder. The
script publishes the port on `127.0.0.1` only.

The mock cannot speak raw EtherCAT, so the server reaches it through its mock
transport (`ETHERCAT_BACKEND=mock`) instead of a network interface.

## Known limitations

- **Not yet run on hardware.** The PySOEM code paths follow PySOEM 1.1.12's API
  and are tested against a fake `pysoem`, not against real slaves.
- **Minimal master.** No distributed clocks, no PDO remapping (slaves keep the
  mapping SOEM reads from CoE or SII), no FoE/EoE, no cable redundancy, and no
  automatic recovery of lost slaves. Working counters are checked per group, not
  per slave.
- **Timing.** The cyclic exchange runs in a Python thread. Expect jitter;
  this is not a real-time master.
- **Raw bytes.** `read_pdo` returns the slave's input bytes. Turning them into
  signals needs the PDO mapping, which `load_esi_file` can show.
- **macOS** cannot grant raw-socket access to a non-root process; run as root
  there.

## Development

See [CONTRIBUTING.md](CONTRIBUTING.md) for setup, the tests and checks CI runs,
and how changes flow into IndustriConnect.
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
