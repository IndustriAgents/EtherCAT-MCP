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
├── ethercat-python/           # the MCP server: pyproject.toml + src/ethercat_mcp/ + tests/
├── ethercat-mock-slave/       # mock slaves (TCP/JSON simulator) + Dockerfile + tests/
├── .github/                   # CI, issue/PR templates, IndustriConnect notification
└── README.md
```

Each of the two folders is its own [`uv`](https://docs.astral.sh/uv/) project
with a committed `uv.lock`. `pytest` and `ruff` are in each project's `dev`
dependency group, which `uv sync` installs.

## What holds the suite together

A change that breaks one of these needs a reason in the pull request:

1. **One response envelope.** Every tool returns
   `{ success, data, error, meta }`, the same as every other IndustriConnect
   server, also for invalid arguments: validate inputs and return
   `success: false` with the reason rather than raising. `meta.backend` names
   the backend that answered.
2. **Read-only by default.** Tools that write PDOs or SDOs go through
   `ETHERCAT_WRITES_ENABLED`; slave state changes need it and
   `ETHERCAT_STATE_CHANGE_ENABLED`. Both default to `false`. A new tool that
   can change equipment must use the same gates, and its MCP annotations must
   say `destructiveHint=True, idempotentHint=False` (read-only tools:
   `readOnlyHint=True`). The gates, and the limits that weaken a safety check,
   are only read from the process environment or `--env-file`, never from the
   implicit `ethercat-python/.env`; no `.env` is ever read from the working
   directory.
3. **Never report what did not happen.** A tool returns `success: true` only if
   the bus (or the mock) confirmed the operation as far as EtherCAT lets a
   master tell (for process data, the frame's working counter and the slave's
   AL state), and never returns made-up device data. If something is not
   implemented, say so with `success: false`.
4. **Never repeat a write.** A device-changing request goes out once. Only
   (re)connecting may be retried. Report `meta.outcome`: `not_sent` only if it
   failed before anything left; `rejected` only for a refusal that by the
   protocol comes before anything is applied (see the README's
   [Safety model](README.md#safety-model)); everything else after the send is
   `unknown` with `meta.request_sent: true`, and tells the caller to read back
   before retrying.
5. **Deadlines and cancellation.** Every device call runs under an overall
   deadline with a `CallToken` (`EthercatMaster._run`). Backends acquire their
   lock through `locked()` and call `token.begin_send()` immediately before
   anything that can change a device, so a call that was cancelled (deadline,
   client cancel, client disconnect) before its send point ends `not_sent` and
   never sends later. An operation SOEM cannot interrupt passes
   `begin_send(min_remaining_s=...)` so that it never starts too close to the
   deadline. Nothing may hang.
6. **Strict arguments.** Positions, offsets, indexes and byte values are strict
   integers (`pydantic.StrictInt`): a boolean is never a number.
7. **A mock for the protocol.** If you add a tool, make it work against
   `ethercat-mock-slave` through the mock transport (`ETHERCAT_BACKEND=mock`)
   and add it to the end-to-end test and to the stdout test's tool list.
8. **Safety tests must be able to fail.** When you add a protection, add a test
   and check that it fails with the protection removed. Mocks that tests start
   must exit when the test run dies (`--exit-on-stdin-eof`, see
   `tests/conftest.py`).

## Getting set up

```bash
git clone https://github.com/IndustriAgents/EtherCAT-MCP.git
cd EtherCAT-MCP/ethercat-python
uv sync
```

`uv run ethercat-mcp` starts the server, which then waits for an MCP client on
stdin (`uv run ethercat-mcp --help` lists the settings). Opening an interface
needs raw-socket privileges (root, or `CAP_NET_RAW` and `CAP_NET_ADMIN` on
Linux; the README's [Real bus](README.md#real-bus) section shows both ways) and
a NIC wired to an EtherCAT segment. Without either, use the mock.

The mock slave runs on its own. From `ethercat-python/`:

```bash
cd ../ethercat-mock-slave
uv sync
uv run ethercat-mock-slave          # 127.0.0.1:6700, see --help for --host/--port
```

## Checks

CI (`.github/workflows/ci.yml`) runs these, and you can run the same ones
locally from the repository root:

```bash
cd ethercat-python
uv sync --locked
uv run --locked ruff check .
uv run --locked ruff format --check .
uv run --locked python -m compileall -q src
uv run --locked ethercat-mcp --help
uv run --locked pytest
# CI also runs the suite with the lowest versions pyproject.toml allows:
uv venv --python 3.11 /tmp/lowest && \
  uv pip install --python /tmp/lowest/bin/python --resolution lowest-direct -e . "pytest>=8.2.0" && \
  /tmp/lowest/bin/python -m pytest

cd ../ethercat-mock-slave
uv sync --locked
uv run --locked ruff check .
uv run --locked ruff format --check .
uv run --locked ethercat-mock-slave --help
uv run --locked pytest
```

The server's tests cover the configuration, the CoE value codec, the ESI
parser, the PySOEM backend against a fake `pysoem` module
(`tests/fake_pysoem.py`, which models SOEM's persistent process image, working
counters and AL error handling), the safety regressions
(`tests/test_safety_regressions.py`), the tools, the FastMCP internals the
server relies on, stdout hygiene with every tool called, and an end-to-end test
that starts the mock and drives `ethercat-mcp` over stdio with the MCP client
SDK. Nothing in either suite opens a network interface. If you change how the
backend calls PySOEM, check the call against PySOEM's source and update the
fake to match.

If you change dependencies, run `uv lock` in that project and commit the
updated `uv.lock`. CI installs with `--locked` and fails if the lockfile is
stale. Keep the floors in `pyproject.toml` honest: the lowest-direct job must
pass. `mcp` stays below 2 because `server.py` uses FastMCP internals.

Then drive the server the way a user would, through the
[MCP Inspector](https://modelcontextprotocol.io/docs/tools/inspector) or a real
client, against the mock. From the repository root, with the mock running:

```bash
cd ethercat-python
npx @modelcontextprotocol/inspector -e ETHERCAT_BACKEND=mock uv run ethercat-mcp
```

Call the tool you changed, and check the envelope, not just the value.

**Do not test against production equipment.** If a change can only be verified
on hardware, say so in the pull request and describe the bench you used
(master NIC, slaves, and ESI revisions).

## House rules for the code

- MCP speaks over **stdio**, so `stdout` belongs to the protocol. All logging
  goes to `stderr`. The CLI also moves file descriptor 1 to stderr before the
  server starts, so a stray `print()` cannot corrupt the session, but do not
  rely on that.
- An error is a returned `{ success: false, error }`, not an exception that
  kills the server or surfaces as a bare tool error. The model needs to be
  told what went wrong.
- Read configuration in `EthercatConfig.from_env()`, never at import time, and
  fail at startup, not at the first tool call, when it is unusable.
- Give every tool a docstring: it is the description clients show.
- Keep tool names and argument names aligned with the other IndustriConnect
  servers.

Questions go to [Discussions](https://github.com/IndustriAgents/EtherCAT-MCP/discussions).

## Pull requests

Say how you tested (no hardware, the mock, or a bench), and whether the change
touches anything that can write to a slave or change its state.

## Code of Conduct

By taking part you agree to the [Code of Conduct](CODE_OF_CONDUCT.md).
