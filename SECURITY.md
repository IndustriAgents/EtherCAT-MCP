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

- **Try it without hardware first.** `ping`, `get_master_status`,
  `list_slaves` and `load_esi_file` never open an interface, so you can wire up
  a client and exercise an agent before it can reach a bus. The mock slave in
  `ethercat-mock-slave/` is a TCP/JSON simulator. The server does not connect
  to it yet (see the [README](README.md#mock-slave)), so when the server is
  pointed at an interface, it is talking to real hardware.
- **Turn writes off unless you mean otherwise.** Write tools are gated by
  `ETHERCAT_WRITES_ENABLED`, which currently **defaults to `true`**. Set it to
  `false` for a read-only server. State changes are gated by
  `ETHERCAT_STATE_CHANGE_ENABLED`, which defaults to `false`. The write and
  state paths are still placeholders today, but the gate is what will stand
  between a model and your outputs once they are finished.
- **Give it a dedicated interface.** `ETHERCAT_INTERFACE` should be a NIC
  wired only to the EtherCAT segment. The master sends broadcast frames on
  whatever interface it is given.
- **Grant the least privilege that works.** Prefer granting the two
  capabilities to the interpreter (the README's
  [Quick start](README.md#quick-start) shows the `setcap` command) over
  running the whole MCP client as root. Remember that a capability set on a
  Python interpreter applies to every script that interpreter runs, in every
  virtual environment built on it, and that `CAP_NET_ADMIN` also lets those
  scripts reconfigure interfaces, routes and firewall rules. Remove the
  capabilities with `setcap -r` when you no longer need them.
- **Do not expose the server beyond the host running the client.** It is a
  stdio process meant to run beside the MCP client, not a network service.
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
