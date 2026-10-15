# industriagents-ethercat-mcp

EtherCAT MCP server for local stdio clients. This npm package includes
the exact Python wheel from the same release; it does not implement a second
protocol stack. Requires Node.js 20+ and [uv](https://docs.astral.sh/uv/).
uv installs a compatible Python and resolves the wheel's dependencies on first use.
Native protocol libraries, network permissions and hardware remain necessary
for the corresponding live backend.

```sh
npx -y industriagents-ethercat-mcp@0.3.0
```

The equivalent Python installation is `uvx --from industriagents-ethercat-mcp==0.3.0 ethercat-mcp`.
Use `ethercat-mcp --help` for configuration. All output on stdout after startup
is MCP JSON-RPC. Environment variables and CLI options are passed unchanged.
Writes are disabled by default. See the repository's [architecture and tool
migration guide](https://github.com/IndustriAgents/EtherCAT-MCP/blob/main/docs/architecture.md).
