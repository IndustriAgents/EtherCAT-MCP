## Summary

<!-- What does this PR do, and why? -->

## Part(s) affected

- [ ] MCP server (`ethercat-python`)
- [ ] Mock slave (`ethercat-mock-slave`)
- [ ] Docs / CI only

## Type of change

- [ ] Bug fix
- [ ] New tool
- [ ] Documentation
- [ ] Refactor / chore

## Does this change what can be written to a device?

- [ ] No
- [ ] Yes — describe the new capability and how it is gated (`ETHERCAT_WRITES_ENABLED` / `ETHERCAT_STATE_CHANGE_ENABLED`):

## Checklist

- [ ] Tools still return the shared `{ success, data, error, meta }` envelope.
- [ ] The CI checks pass locally: ruff and pytest in both projects (see [CONTRIBUTING.md](https://github.com/IndustriAgents/EtherCAT-MCP/blob/main/CONTRIBUTING.md#checks)).
- [ ] New or changed tools work against the mock (`ETHERCAT_BACKEND=mock`) and are covered by tests.
- [ ] Nothing reports `success: true` for an operation the bus (or the mock) did not confirm.
- [ ] If I changed dependencies, I ran `uv lock` and committed the updated `uv.lock`.
- [ ] I said below what I tested against: no hardware, the mock, or a bench (never production equipment).
- [ ] `stdout` is still clean — all logging goes to `stderr`.
- [ ] I updated the README where relevant.

## How to test

<!-- The exact commands and tool calls a reviewer can run. -->
