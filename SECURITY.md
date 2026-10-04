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
  are gated by `ETHERCAT_WRITES_ENABLED` and state changes by
  `ETHERCAT_STATE_CHANGE_ENABLED`. Both default to `false`. With both off, the
  server never moves a slave above PRE-OP and never writes outputs or CoE
  objects. Turning on state changes lets the model put slaves into OP, where
  outputs are live; turning on writes lets it set those outputs and change CoE
  parameters.
- **Know that sudo drops your environment.** A gate you exported in your shell
  does not reach a server started with sudo; only the variables on the sudo
  command line, an `--env-file`, or a `.env` in the working directory do.
  `ethercat-mcp --print-config` shows what the process really sees.
- **A scan is not passive.** `scan_network` makes this server the bus master:
  SOEM resets every slave on the segment to INIT and then requests PRE-OP.
  Never connect the server's interface to a segment that another master is
  running, for example through a free port of a running line.
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
  returns only parsed ESI fields and does not resolve XML entities or DTDs.
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
