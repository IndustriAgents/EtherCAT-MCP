# ethercat-mock-slave

Simulated EtherCAT slaves for testing the EtherCAT MCP server without hardware. The mock does not speak raw EtherCAT: it answers newline-delimited JSON over TCP, and the server in [`../ethercat-python`](../ethercat-python) reaches it through its mock transport (`ETHERCAT_BACKEND=mock`). Every answer the server gets this way is marked `meta.backend: "mock"` and `meta.simulated: true`.

It uses only the Python standard library.

## Usage

```bash
uv sync
uv run ethercat-mock-slave
```

Then start the server with `ETHERCAT_BACKEND=mock` (and `ETHERCAT_MOCK_HOST` / `ETHERCAT_MOCK_PORT` if you changed them). See [Without hardware: the mock](../README.md#without-hardware-the-mock).

Flags (`--host`, `--port`, `--update-interval`, `--verbose`, `--power-on-state`) or the matching `MOCK_ETHERCAT_HOST`, `MOCK_ETHERCAT_PORT`, `MOCK_ETHERCAT_UPDATE_INTERVAL`, `MOCK_ETHERCAT_VERBOSE` and `MOCK_ETHERCAT_POWER_ON_STATE` environment variables set host (default `127.0.0.1`), port (default `6700`; `0` picks a free port and the log line shows which), how often the analog inputs change (seconds, default `1.0`), per-request logging, and the AL state the slaves start in (default `INIT`; `SAFEOP` or `OP` simulates a segment that another master is running, which the server refuses to scan unless both of its gates are on). Logs go to stderr. To run it in Docker instead, use `./run-container.sh [HOST_PORT]`, which publishes the port on `127.0.0.1` only. Anyone who can reach the port can change the simulated outputs, so keep it on loopback.

## What it simulates

| Position | Name | Process data | CoE |
|----------|------|--------------|-----|
| 0 | EK1100 coupler | none | no mailbox |
| 1 | EL1008, 8 digital inputs | 1 input byte, wired to the EL2008 outputs | no mailbox |
| 2 | EL2008, 8 digital outputs | 1 output byte | no mailbox |
| 3 | EL3102, 2 analog inputs | 8 input bytes (status, value per channel; values move over time) | identity objects 0x1000-0x1018, values 0x6000:11/0x6010:11, writable settings 0x8000:06, 0x8000:15, 0x8010:06 and 0x8010:15 (subindexes in hex) |

- Slaves power up in INIT (or `--power-on-state`). `scan` behaves like SOEM's `config_init`: every slave goes to INIT, then PREOP.
- State changes follow the EtherCAT state machine (INIT -> PREOP -> SAFEOP -> OP, and back down). An invalid request (for example PREOP -> OP) is refused with AL status `0x0011` and the slave stays where it was.
- Inputs can be read in SAFEOP and OP. Outputs can be written only in OP. A slave drives its outputs only in OP and drops them to zero when it leaves OP.
- Like a real master's process image (SOEM never clears it), the mock's output image is **not** cleared when a slave leaves OP or by a scan: a slave that re-enters OP applies whatever the image still holds. The master has to zero it (`clear_outputs`), as the server does after a scan, before a slave enters OP, and whenever a slave ends up outside OP.
- CoE needs a mailbox and PREOP or higher. Unknown objects, unknown subindexes, writes to read-only objects and wrong lengths return the matching SDO abort codes (`0x06020000`, `0x06090011`, `0x06010002`, `0x06070010`).

Identity values are illustrative and the objects are simplified; it is a test double, not a conformance reference.

## JSON protocol

One JSON object per line in each direction. Replies are `{"success": true, "data": ...}` or `{"success": false, "error": "..."}` (plus `abort_code` for SDO aborts).

| Request | Reply `data` |
|---------|--------------|
| `{"op": "ping"}` | `{"mock": true, "slaves": 4}` |
| `{"op": "scan"}` | list of slaves: `position`, `name`, `vendor_id`, `product_code`, `revision`, `input_size`, `output_size`, `has_mailbox`, `state`, `state_code`, `al_status_code`, `al_status` |
| `{"op": "read_state"}` | list of `position`, `state`, `state_code`, `al_status_code`, `al_status` (also before a scan; the server's live-segment check uses it) |
| `{"op": "set_state", "position": 2, "state": "SAFEOP"}` (or `"state_code": 4`) | the slave's new state entry |
| `{"op": "read_pdo", "position": 1, "offset": 0, "length": 1}` | `{"position", "raw_data_hex", "state"}` (`offset`/`length` optional) |
| `{"op": "write_pdo", "position": 2, "offset": 0, "data": [165]}` | `{"position", "written_bytes", "state"}` |
| `{"op": "clear_outputs", "position": 2}` | `{"position", "cleared_bytes", "state"}` (zeroes that slave's output image, any state) |
| `{"op": "read_sdo", "position": 3, "index": 4120, "subindex": 1}` | `{"position", "data_hex", "size"}` |
| `{"op": "write_sdo", "position": 3, "index": 32768, "subindex": 6, "data_hex": "01"}` | `{"position", "written_bytes"}` |

## Tests

```bash
uv sync
uv run pytest
uv run ruff check . && uv run ruff format --check .
```
