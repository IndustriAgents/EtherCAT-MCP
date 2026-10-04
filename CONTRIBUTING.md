# Contributing to EtherCAT MCP

Thanks for taking an interest. This repository holds the EtherCAT MCP server,
a Python EtherCAT master built on [PySOEM](https://github.com/bnjmnp/pysoem),
plus a mock slave. It is one of the protocol servers of the
[IndustriConnect](https://github.com/IndustriAgents/IndustriConnect) suite, and
most of what follows is about keeping it consistent with the others.

## Where changes go

This repository is where the EtherCAT server is developed. IndustriConnect
ships it as the `EtherCAT-Project` git submodule, which tracks this repo's
`main` branch. So:

- Open issues and pull requests **here**, not in IndustriConnect. A change
  made inside IndustriConnect's `EtherCAT-Project/` folder would only move its
  submodule pointer, and the change itself would be lost.
- Every push to `main` notifies IndustriConnect (see
  `.github/workflows/notify-industriconnect.yml`) so that it can bump its
  submodule pointer.

## Repository layout

```text
EtherCAT-MCP/                  # IndustriConnect mounts this as EtherCAT-Project/
├── ethercat-python/           # the MCP server: pyproject.toml + src/ethercat_mcp/
├── ethercat-mock-slave/       # mock slave (TCP/JSON simulator) + Dockerfile
├── .github/                   # CI, issue/PR templates, IndustriConnect notification
└── README.md
```

Each of the two folders is its own [`uv`](https://docs.astral.sh/uv/) project
with a committed `uv.lock`.

## What holds the suite together

A change that breaks one of these needs a reason in the pull request:

1. **One response envelope.** Every tool returns
   `{ success, data, error, meta }`, the same as every other IndustriConnect
   server. (`read_sdo` and `write_sdo` still break this for an `index` that is
   not a number, as noted in the README's
   [known limitations](README.md#known-limitations).)
2. **Writes are gated.** Tools that write PDOs or SDOs go through
   `ETHERCAT_WRITES_ENABLED`, and slave state changes go through
   `ETHERCAT_STATE_CHANGE_ENABLED`. A new tool that can change equipment must
   use the same gates. (The suite's rule is read-only by default. Here,
   `ETHERCAT_WRITES_ENABLED` still defaults to `true`, as noted in the README's
   [known limitations](README.md#known-limitations).)
3. **A mock for the protocol.** If you add a tool, think about how it can be
   exercised without a plant. The EtherCAT mock does not speak to the server
   yet, so closing that gap is welcome work.

## Getting set up

```bash
git clone https://github.com/IndustriAgents/EtherCAT-MCP.git
cd EtherCAT-MCP/ethercat-python
uv sync
```

`uv run ethercat-mcp` starts the server, which then waits for an MCP client on
stdin. To talk to it, use the MCP Inspector as shown under [Checks](#checks).
Opening an interface needs raw-socket privileges (root, or `CAP_NET_RAW` and
`CAP_NET_ADMIN` on Linux; the README's
[Quick start](README.md#quick-start) shows both ways) and a NIC wired to an
EtherCAT segment. Without either, the server still starts. `ping`,
`get_master_status`, `list_slaves` and `load_esi_file` work, and the bus tools
return `success: false` with the reason.

The mock slave runs on its own. From `ethercat-python/`:

```bash
cd ../ethercat-mock-slave
uv sync
uv run ethercat-mock-slave          # 127.0.0.1:6700, see --help for --host/--port
```

## Checks

There is no automated test suite yet. CI (`.github/workflows/ci.yml`) runs
these checks, and you can run the same ones locally from the repository root:

```bash
cd ethercat-python
uv sync --locked
uv run --locked python -m compileall -q src
uv run --locked python -c "import ethercat_mcp, ethercat_mcp.cli, ethercat_mcp.esi_parser"
# CI also starts `ethercat-mcp` over stdio and checks initialize, tools/list and ping.

cd ../ethercat-mock-slave
uv sync --locked
uv run --locked python -m compileall -q ethercat_mock_slave.py
uv run --locked ethercat-mock-slave --help
# CI also starts the mock in-process and round-trips scan/read_pdo/write_pdo.
```

If you change dependencies, run `uv lock` in that project and commit the
updated `uv.lock`. CI installs with `--locked` and fails if the lockfile is
stale.

Then drive the server the way a user would, through the
[MCP Inspector](https://modelcontextprotocol.io/docs/tools/inspector) or a real
client. From the repository root:

```bash
cd ethercat-python
npx @modelcontextprotocol/inspector uv run ethercat-mcp
```

Call the tool you changed, and check the envelope, not just the value.

**Do not test against production equipment.** If a change can only be verified
on hardware, say so in the pull request and describe the bench you used
(master NIC, slaves, and ESI revisions).

## House rules for the code

- MCP speaks over **stdio**, so `stdout` belongs to the protocol. All logging
  goes to `stderr`. A stray `print()` corrupts the session.
- An error is a returned `{ success: false, error }`, not an exception that
  kills the server or surfaces as a bare tool error. The model needs to be
  told what went wrong.
- Fail at startup, not at the first tool call, when configuration is unusable.
- Keep tool names and argument names aligned with the other IndustriConnect
  servers.

## Pull requests

Say how you tested (no hardware, the mock, or a bench), and whether the change
touches anything that can write to a slave or change its state.

## Code of Conduct

By taking part you agree to the [Code of Conduct](CODE_OF_CONDUCT.md).
