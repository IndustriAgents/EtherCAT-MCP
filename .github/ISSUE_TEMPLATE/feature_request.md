---
name: Feature request
about: Suggest a new tool or an improvement to the EtherCAT server or mock
title: "[Feature] "
labels: enhancement
assignees: ""
---

**What problem does this solve?**
The use case, from the point of view of someone asking an agent to do something.

**Which part?**
- [ ] MCP server (`ethercat-python`)
- [ ] Mock slave (`ethercat-mock-slave`)

**Proposed solution**
If it is a new tool: its name, arguments, and what it returns. This server is
part of the IndustriConnect suite, where tool names and arguments are kept
consistent across all protocol servers, so say how it lines up with the
equivalent tool elsewhere in the suite.

**Does it write to a device or change slave state?**
- [ ] No — read-only
- [ ] Yes — it changes physical equipment

**Can the mock answer it?**
What the mock slave would need to be able to simulate for this to be testable
without real hardware.

**Additional context**
Links to the EtherCAT / ETG specification, example slaves or ESI files, or
related issues.
