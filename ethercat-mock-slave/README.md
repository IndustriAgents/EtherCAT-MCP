# ethercat-mock-slave

Minimalist EtherCAT slave simulator for quick MCP smoke tests. Instead of speaking raw EtherCAT, the current scaffold exposes a TCP JSON API that mimics network scans and PDO buffers until a proper SOEM-based mock is implemented.

The MCP server in [`../ethercat-python`](../ethercat-python) does not connect to this mock yet. The server drives a network interface through PySOEM, while this mock answers JSON over TCP. Use it to exercise the JSON API directly, or as the starting point for a SOEM-based simulator.

## Usage

```bash
uv sync
uv run ethercat-mock-slave
```

Defaults:

- Listens on `127.0.0.1:6700`
- Accepts newline-delimited JSON commands:
  - `{ "op": "scan" }`
  - `{ "op": "read_pdo", "position": 0, "length": 8 }`
  - `{ "op": "write_pdo", "position": 0, "data": [1,0,0,0] }`

Flags (`--host`, `--port`, `--update-interval`, `--verbose`) or the matching `MOCK_ETHERCAT_HOST`, `MOCK_ETHERCAT_PORT`, `MOCK_ETHERCAT_UPDATE_INTERVAL` and `MOCK_ETHERCAT_VERBOSE` environment variables adjust host, port, update interval and logging. To run it in Docker instead, use `./run-container.sh [HOST_PORT]`, which publishes the port on `127.0.0.1` only.

Extend `ethercat_mock_slave.py` later to hook into real SOEM slave stacks, state machines, and ESI generation (planned, not implemented yet).
