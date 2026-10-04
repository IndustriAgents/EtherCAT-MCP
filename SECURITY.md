# Security Policy

## What this server is, in security terms

The EtherCAT MCP server is a bridge between a language model and an EtherCAT
network. It speaks MCP over stdio to its client and, through
[PySOEM](https://github.com/bnjmnp/pysoem), raw EtherCAT frames on a network
interface. That puts the trust boundary *inside* the server: whatever the model
decides to call, the master is asked to do.

EtherCAT has no authentication or encryption of its own. It was designed for a
physically isolated segment, so anything that can put frames on that wire can
read and write process data. Opening the interface also needs raw-socket
access: root, or on Linux the `CAP_NET_RAW` and `CAP_NET_ADMIN` capabilities.

So treat this server as equipment on the control network, not as an
application on the office network.

## Running it safely

- **Try it without hardware first.** Run the server with
  `ETHERCAT_BACKEND=mock` against the mock slave in `ethercat-mock-slave/`
  (a TCP/JSON simulator). Every answer it produces is marked
  `meta.backend: "mock"` and `meta.simulated: true`. With the default
  `ETHERCAT_BACKEND=pysoem`, the server talks to whatever is wired to
  `ETHERCAT_INTERFACE`: real hardware.
- **Keep writes and state changes off unless you mean otherwise.** Write tools
  are gated by `ETHERCAT_WRITES_ENABLED`; state changes need it and
  `ETHERCAT_STATE_CHANGE_ENABLED`. Both default to `false`. With them off, the
  server never moves a slave above PRE-OP and never writes outputs or CoE
  objects. Turning on state changes lets the model put slaves into OP, where
  outputs are live; turning on writes lets it set those outputs and change CoE
  parameters. A write is sent once and never repeated; if its answer is lost,
  the result says `meta.outcome: "unknown"`, and you should read back before
  retrying.
- **No `.env` from the working directory, and none that weakens safety.**
  When an MCP client starts the server, the working directory is the client's
  project, and settings such as the interface or the mock address decide where
  a write goes. The server never reads a `.env` there or in a parent directory.
  The only file it reads by itself is `ethercat-python/.env` of a source
  checkout (none for an installed package), and that file may only set
  `ETHERCAT_*` and `SLAVE_MAP_FILE`, never the gates
  (`ETHERCAT_WRITES_ENABLED`, `ETHERCAT_STATE_CHANGE_ENABLED`) or the limits
  `ETHERCAT_PD_WATCHDOG_MS` and `ETHERCAT_EXPECTED_WKC`. Anything else in it is
  ignored with a warning, whatever the letter case. Set the gates in the process
  environment or in a file you pass with `--env-file`. See
  `ethercat-python/.env.example`.
- **Know that sudo drops your environment.** A gate you exported in your shell
  does not reach a server started with sudo; only the variables on the sudo
  command line or in an `--env-file` do. `ethercat-mcp --print-config` shows
  what the process really sees.
- **A scan is not passive.** `scan_network` makes this server the bus master:
  SOEM resets every slave on the segment to INIT and then requests PRE-OP.
  The server first reads the AL state of the whole segment and refuses if a
  slave is in SAFEOP or OP, unless both gates are on, but a segment that another
  master is still bringing up (slaves in INIT or PRE-OP) passes that check. Never connect the
  server's interface to a segment that another master is running, for example
  through a free port of a running line.
- **Do not count on the watchdog alone.** On shutdown the server requests INIT.
  If it is killed outright (SIGKILL), outputs stay as they were until the
  process-data watchdog expires (`ETHERCAT_PD_WATCHDOG_MS`, set on every slave
  with outputs; PySOEM marks this experimental), and only on slaves whose sync
  manager enables that watchdog.
- **Give it a dedicated interface.** `ETHERCAT_INTERFACE` should be a NIC
  wired only to the EtherCAT segment. The master sends broadcast frames on
  whatever interface it is given.
- **Grant the least privilege that works.** Prefer granting the two
  capabilities to the interpreter (the README's
  [Real bus](README.md#real-bus) section shows the `setcap` command) over
  running the whole MCP client as root. Remember that a capability set on a
  Python interpreter applies to every script that interpreter runs, in every
  virtual environment built on it, and that `CAP_NET_ADMIN` also lets those
  scripts reconfigure interfaces, routes and firewall rules. Remove the
  capabilities with `setcap -r` when you no longer need them.
- **Do not expose the server beyond the host running the client.** It is a
  stdio process meant to run beside the MCP client, not a network service. The
  mock slave listens on `127.0.0.1` by default; keep it there.
- **ESI files are read with the server's privileges.** `load_esi_file` opens
  the path the client passes (as root, if you run the server as root). It
  returns only parsed ESI fields. A file that declares a DOCTYPE or ENTITY is
  refused before parsing (libxml2 expands internal entities in attribute values
  even with entity resolution off, so a 100 KB file could cost seconds and
  hundreds of MB), as is a file larger than `ETHERCAT_ESI_MAX_MB`. Parsing runs
  off the MCP event loop with a deadline.
- **Every device call has a deadline.** A device or mock call that does not
  finish within its overall deadline ends the tool call: a read with an error,
  a write with `meta.outcome: "unknown"`.
- **Never point it at a safety system.** Functional-safety traffic (for
  example Safety over EtherCAT / FSoE) is out of scope for this project.

## Reporting a vulnerability

Please report privately, by email to hi@industriagents.com or through
[GitHub private vulnerability reporting](https://github.com/IndustriAgents/EtherCAT-MCP/security/advisories/new).

Please do not open a public issue for a vulnerability.

Include the version or commit, what an attacker would gain, and a reproduction
if you have one. We will acknowledge within a week and keep you updated as we
work on a fix. This server also ships inside
[IndustriConnect](https://github.com/IndustriAgents/IndustriConnect) as the
`EtherCAT-Project` submodule. Report it here even if you found it there.

## Scope

In scope: the MCP server (`ethercat-python/`) and the mock slave
(`ethercat-mock-slave/`, including its Dockerfile) in this repository.

Out of scope: weaknesses of the EtherCAT protocol itself (the lack of
authentication is a property of EtherCAT), vulnerabilities in third-party
libraries such as PySOEM/SOEM or the MCP SDK (please report those upstream),
and vendor slave firmware.
