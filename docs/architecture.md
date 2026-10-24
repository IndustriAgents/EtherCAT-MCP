# Architecture and migration (0.3.0)

This repository is an independent protocol MCP server. It follows OPCUA-MCP's
instance-owned composition, explicit MCP contract and application/adapter split.
It has no runtime dependency on another IndustriAgents protocol repository or
on IndustriConnect-MCPs. The umbrella repository can pin this repository as a
submodule and launch its released command.

## Execution path

```mermaid
flowchart LR
  Client[MCP client] -->|stdio| Contract[Protocol contract and dispatcher]
  Contract --> Execution[Instance policy, admission and audit]
  Execution --> Application[Protocol application operations]
  Application --> Adapter[Native protocol adapter]
  Adapter --> Device[Configured endpoint or bus]
```

`server.py` composes one server instance and its resources. `protocol/` owns
registration, schema dispatch, SDK request context access and wire errors.
`application/` owns reads, writes, discovery, diagnostics and metadata operations.
`domain/` holds protocol types, error/outcome vocabulary, structural ports and
policy values. `adapters/` contains the native driver and owns its connection,
retry eligibility, send tracking and cancellation fences. `infrastructure/`
contains audit and execution-context support. Compatibility modules at the old
Python import paths forward to the implementation rather than owning another
connection. Some established config/codec/metadata helpers keep their root
module paths to avoid unnecessary public import breaks.

The supported deployment is local stdio with one caller context and one
configured endpoint/bus per process. Instance ownership is not a claim of
multi-tenant authorization or remote HTTP transport support. Modbus and MQTT
provide `create_server(...)` with explicit per-instance configuration; the
other protocols keep their existing server constructors with injected clients
or masters. The CLI default instance exists only for compatibility.

## Tool contract

`contract/tools.json` is the reviewed contract, including JSON schemas, routing,
legacy names and the result envelope. Its identical packaged resource is
`ethercat-python/src/ethercat_mcp/_contract/tools.json`.
Run `python scripts/verify_contract.py` to reject drift. There is no second
TypeScript implementation: npm carries the same wheel as the PyPI release.

| Advertised tool | Modes | Effect |
|---|---|---|
| `read_pdo` | `address`, `alias` | Read/local |
| `write_pdo` | `address`, `alias` | Changes state or consumes events |
| `scan_network` | No mode selector | Changes state or consumes events |
| `get_slave_info` | No mode selector | Read/local |
| `test_slave_communication` | No mode selector | Read/local |
| `read_sdo` | No mode selector | Read/local |
| `write_sdo` | No mode selector | Changes state or consumes events |
| `set_slave_state` | No mode selector | Changes state or consumes events |
| `list_slaves` | No mode selector | Read/local |
| `load_esi_file` | No mode selector | Read/local |
| `ping` | No mode selector | Read/local |

A mode selects one protocol operation. Parameters from different modes cannot
be mixed; unknown parameters fail before dispatch. Alias/tag modes resolve
through the existing configured map and preserve its scaling and validation.
Legacy names remain callable but are omitted from `tools/list`. Names retained
in both catalogs accept the old address form without the new selector for
migration. Those legacy forms and direct tools without a mode selector retain native
argument validation and coercion. Unknown arguments are rejected before native
validation, including on every compatibility route.

| Legacy name | Advertised replacement |
|---|---|
| `read_slave_by_alias` | `read_pdo` |
| `write_slave_by_alias` | `write_pdo` |
| `get_master_status` | `ping` |

Every tool returns `{success, data, error, meta}`. On the wire, `success:false`
sets MCP `isError:true`; structuredContent and the JSON text describe the same
result. Writes preserve `meta.outcome` and `meta.request_sent` from the native
adapter. A timeout/cancellation after possible dispatch is uncertain, never
converted to not_sent. There is no new generic retry loop. In particular,
retrying an unknown control outcome can apply a physical action twice.

## Bounded requests and failure outcomes

The contract declares `limits.maxNestingDepth` (32), `maxContainerItems`
(10,000), and `maxTotalValues` (50,000). An iterative walk checks these bounds,
JSON types, finite numbers and valid Unicode before schema validation, policy
admission or native dispatch. The configured `max_request_bytes` then limits
serialized arguments, including escaping. These checks bound application work
after the MCP SDK has decoded a transport frame; they are not a transport-frame
size limit.

Upfront native signature validation preserves existing coercion and proves a
refused call was `not_sent`. A handler or result-conversion failure, including a
Pydantic validation failure, does not prove that: an unexpected control error
returns `outcome: "unknown"`, `request_sent: null` and a generic MCP error without
replaying the operation or exposing its payload. Established adapter outcomes
remain authoritative. Cancellation releases admission and audits an unknown
outcome when no result was observed. If final audit persistence fails, JSON text
and structured content both carry `meta.audit_error` without changing the
observed control outcome.

## Deployment policy and audit

Set `ETHERCAT_POLICY_FILE` to an explicit JSON policy file. Unknown fields
and invalid types fail at server construction. Without a policy file the native
protocol write gates continue to apply; writes are disabled by default.

```json
{
  "read_only": true,
  "allowed_tools": null,
  "allowed_operations": null,
  "max_in_flight": 64,
  "max_request_bytes": 1048576,
  "audit_path": null
}
```

`allowed_tools` restricts canonical tool names; `allowed_operations` restricts
the concrete handler names in the contract's routes. Null means unrestricted;
an empty array denies everything. Legacy names map to the same canonical
policy, so they cannot bypass it. `read_only` also denies non-read-only
operations such as scans or event-consuming polls. This is tool/operation
policy, not resolved-address or engineering-value authorization. Existing
protocol maps, value limits and write/system gates remain authoritative for
those checks. No new OPC UA-style namespace or physical interlock semantics
are implied for these different protocols.

Admission rejects excess calls immediately, with one additional slot reserved
for a local read-only diagnostic. It does not create an unbounded wait queue.
The native adapter still owns ordering and pending native I/O. Cancellation
releases the MCP admission slot; a cancelled worker thread is not assumed to
have stopped, and native send fences remain responsible for that case.

When `audit_path` is set, a write intent is appended and fsynced before dispatch.
Failure to persist that intent refuses dispatch. The final observed outcome is
appended with the same call ID; a missing result after process failure means
unknown, not success. Failure to persist a result is logged and marked
`meta.audit_error`, without resending the operation or changing its outcome.
Audit records omit payloads and credentials. Manage file retention externally;
this append-only log is not a tamper-proof external audit service.

Policy documents are read only from regular files, with a 64 KiB read bound and
at most 16 JSON nesting levels. Duplicate fields are rejected. The size bound
still applies if a file grows during the read. Audit destinations must also be
regular files; devices and pipes are refused before any audit record is written.
A FIFO cannot block policy loading or audit admission while waiting for a peer.

## Protocol capability limits

Live EtherCAT uses PySOEM and requires a compatible NIC and operating-system privileges. Network scanning and state transitions can disturb a running bus. PDO and SDO remain separate operations. Fake SDK and mock tests do not establish hardware timing, watchdog behavior or device compatibility.

## Packages

```sh
uvx --from industriagents-ethercat-mcp==0.3.0 ethercat-mcp
npx -y industriagents-ethercat-mcp@0.3.0
```

The distribution is deliberately prefixed: several generic protocol names on
PyPI belong to other projects. Python import names and command names remain
`ethercat_mcp` and `ethercat-mcp`. npm requires Node 20+ and uv; uv provisions a
compatible Python on first run. npm bundles a version-checked wheel and passes
stdio, signals, environment and CLI arguments through to it. Network access is
needed on first use to resolve Python dependencies. Native drivers/permissions
are still required for live operation. The mock backend is not selected merely
because a native dependency or device is unavailable.

## Verification and release

The regression suite retains native-send counting, reply-loss, cancellation,
configuration-gate and stdio checks. New architecture tests exercise all
consolidated routes, unknown/mixed arguments, legacy-policy bypass, independent
admission state, durable intent failure and MCP error envelopes. npm tests
check the bundled version, argument boundaries, exit status and missing-uv
errors. See [release.md](release.md) for the registry workflow.

## Cross-review follow-up

Control preparation errors remain `not_sent`, while an unclassified error from
a master port after invocation is `unknown` with `request_sent: null`. The
blocking-backend facade uses its existing call token to distinguish failures
before and after its send point, and cancels failed tokens so work cannot
continue later. Native classified refusals retain their original outcome.

The mock bridge checks the acknowledged byte count for both PDO and SDO writes.
Missing, non-integer or partial counts are uncertain failures, never successful
writes. An output-image cleanup failure after a completed scan cannot make the
whole scan `not_sent`; it reports an unknown outcome without resending.

Invalid or missing deployment policy files are startup configuration errors:
the CLI exits with status 2, emits a diagnostic on stderr, and leaves stdout
free for MCP transport.
