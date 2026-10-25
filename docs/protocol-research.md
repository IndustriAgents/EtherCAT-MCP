# EtherCAT protocol assessment and implementation evidence

Research date: 2026-10-25. Baseline: `4242c4c`. This assessment concerns the
supported PySOEM and TCP mock backends, not every EtherCAT profile or device.

## Sources and boundaries

| Primary source | Version / sections inspected | Consequence |
| --- | --- | --- |
| [PySOEM Master](https://pysoem.readthedocs.io/en/latest/master.html) | Online 1.1.14-dev1 documentation; `config_init`, `config_map`, process data, expected WKC, state checks | Scan, mapping, cyclic exchange and state verification are distinct responsibilities. |
| [PySOEM CdefSlave](https://pysoem.readthedocs.io/en/latest/cdef_slave.html) | Online 1.1.14-dev1 documentation; SDO, watchdog, state and mailbox APIs | CoE is byte-oriented; a successful library call is not proof of a physical process outcome. |
| [PySOEM source](https://github.com/bnjmnp/pysoem) | Repository reference; local lock pins 1.1.12 | Development documentation is not an upgrade instruction. Tests target the installed API and fake implementation. |
| [ETG technology reference](https://www.ethercat.org/en/technology.html) | Access attempt returned HTTP 403 | No claim of standards conformance is based on inaccessible ETG material. |

The native implementation uses a persistent process-data cycle and serializes
blocking master work. MCP requests select operations and observe results; the
model is not the cycle scheduler. The existing native fences retain ownership
while an abandoned worker is still executing. This preserves OPCUA-MCP's
separation of application operations, native execution and uncertain outcomes.

## Capability and gap matrix

| Workflow / canonical tool | Supported implementation | Authorization / outcome | Evidence / remaining boundary |
| --- | --- | --- | --- |
| Inspect backend: `ping` | Local status and explicit capability inventory added | No interface opened; config gates are not policy grants | MCP schema test and installed wheel/npm smoke |
| Enumerate: `scan_network` | PySOEM `config_init`, mock scan | State-changing operation governed by MCP policy; not a passive network scan | Existing scan/state failure and cleanup tests; live bus validation unavailable |
| Inspect: `get_slave_info`, `test_slave_communication` | Native identity/state, optional CoE diagnostic | Reads only; a CoE failure is not invented as a value | Fake PySOEM and mock tests |
| Read process image: `read_pdo` | Fresh cycle, state, WKC and bit-layout checks | Invalid/stale cycle fails; aliases preserve addressing | Native backend tests; real NIC and cycle timing not verified |
| Write process image: `write_pdo` | Gated native write with cycle confirmation | No replay after possible send; WKC does not verify an actuator | Existing cancellation/send-count and unknown-outcome tests |
| CoE: `read_sdo` | Typed full-object upload with explicit 1..4096-byte buffer | Validate type/buffer before access; budget selected size; reject overflow without truncation | Native-size, mock, real MCP and cancellation tests; raw BYTES remains available |
| CoE: `write_sdo` | Encoded and gated native download | Single dispatch, abort/error classification | Native SDO timeout/abort tests |
| State: `set_slave_state` | Explicit native state request and verification | Both write and state gates required | Native and MCP gate tests |
| Metadata: `list_slaves`, `load_esi_file` | Local alias map and bounded ESI parsing | No proof of reachability from metadata | Map/parser tests |
| Distributed-clock engineering, firmware/FoE, EEPROM, full EMCY/event workflows | Not exposed | No generic raw-command bypass | Outside current tool contract; require device profile and operational requirements |

## Changes and compatibility

`ping.data.capabilities` reports `protocol`, actual backend name, `simulated`,
`scope: "backend_support"`, `device_verified: false`, canonical operations and
prerequisites. Each operation separates `supported` from `enabled`. The latter
reflects native configuration gates only; policy, access rights, bus state and
mapping still determine whether a call may run. An unknown injected backend
is not advertised as a known native implementation.

An invalid SDO `data_type` formerly contacted the device before failing during
decoding. It now fails before the call. IEEE nonfinite values formerly escaped
as non-JSON floats, and BOOL byte values other than 0/1 became `true`. They now
produce explicit decode failures. Use `data_type: "BYTES"` when investigating
an unusual device representation; no measurement is substituted.

INT64/UINT64 values outside ±(2^53−1) now use exact decimal strings with
`value_encoding: "decimal_string"`; safe 64-bit integers remain numeric with
`value_encoding: "json_number"`. This also applies to inferred UINT64 values.
Clients that assumed every decoded integer is a JSON number must handle this
explicit encoding to avoid precision loss in JavaScript and similar clients.

Tool names, import paths, write defaults and packaging remain compatible.
Successful status results now include the documented capability object.
The installed-artifact smoke tests compare descriptions, schemas and annotations,
check text/structured equality, and invoke local status in a sanitized environment.

## Verification and deployment limits

Baseline checks passed before editing. The full workspace verifier is run for
this repository: contract parity, locked dependencies, lint/format, Python and
mock suites, wheel/sdist build, npm tests/pack, installed Python/npm MCP sessions,
metadata checks and source/artifact consistency. Tests use fake SDKs and loopback
fixtures; no real interface was opened.

No compatible EtherCAT interface, slaves, approved bus topology or authorized
hardware test environment was supplied. Hardware acceptance must verify each
supported device's state transitions, PDO mapping, WKC, watchdog and timing under
load. Software tests do not establish hard real-time or functional safety claims.

## Second-pass larger SDO uploads and roadmap

The pinned [PySOEM 1.1.12 binding](https://github.com/bnjmnp/pysoem/blob/1.1.12/src/pysoem/pysoem.pyx)
implements `sdo_read(..., size=0)` with a 256-byte default buffer, allocating the
requested size otherwise and returning the native actual byte count after error
checks. The [public slave API](https://pysoem.readthedocs.io/en/stable/cdef_slave.html)
documents this size argument for larger objects. The MCP now exposes it as
`max_bytes` (1..4096, default 256). This additive parameter applies to canonical
and legacy calls and both backends; it requests a complete upload, not a prefix.
Fixed-type length constraints are checked before bus access. Native timeout
budgeting uses the selected upper bound; oversized backend results fail rather
than truncate. The same native lock survives caller cancellation until the
actual upload returns. Successful metadata records the buffer bound/completeness.

Prioritized remaining work:

1. Hardware acceptance of actual mailbox sizes, segmented transfers, aborts and
   deadlines, alongside existing PDO state/WKC/watchdog timing requirements.
2. Bounded device-profile diagnostics and EMCY observation using documented native
   APIs, with explicit loss/freshness and device-specific interpretation.
3. Evaluate distributed-clock engineering and FoE only with concrete commissioning
   workflows, supported device profiles and operational approval requirements.
