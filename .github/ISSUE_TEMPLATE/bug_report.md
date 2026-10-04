---
name: Bug report
about: Report a problem with the EtherCAT MCP server or the mock slave
title: "[Bug] "
labels: bug
assignees: ""
---

**Which part?**
- [ ] MCP server (`ethercat-python`, the `ethercat-mcp` command)
- [ ] Mock slave (`ethercat-mock-slave`)

**Describe the bug**
What happens, and what you expected instead.

**To reproduce**
The MCP client you used (Claude Desktop / Claude Code / Cursor / Inspector /
IndustriConnect's mcp-manager-ui), the tool you called, and the arguments you
passed.

1.
2.
3.

**Tool output**
```json
paste the { success, data, error, meta } envelope, or the error
```

**What was on the other end?**
- [ ] Nothing — a tool that does not touch the bus (`ping`, `list_slaves`, `load_esi_file`, ...)
- [ ] The mock slave from this repo (`ETHERCAT_BACKEND=mock`, `meta.backend: "mock"`)
- [ ] Real EtherCAT slaves (`meta.backend: "pysoem"`) — vendor, model, and ESI revision:

**Environment**
- OS:
- Python version:
- `ethercat-mcp --version`:
- `ETHERCAT_BACKEND`, `ETHERCAT_INTERFACE`, and how raw-socket access was granted (root / `setcap` capabilities / n/a):
- Commit SHA (or the IndustriConnect commit, if you run it as `EtherCAT-Project`):

**Additional context**
Slave positions, PDO/SDO indexes, your slave map, the ESI file, stderr logs —
whatever would let someone else reproduce it.
