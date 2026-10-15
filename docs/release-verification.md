# 0.3.0 local release verification

Verified on 2026-10-15. Package: `industriagents-ethercat-mcp`.

This records local build verification, not registry publication. Check the
GitHub Actions runs and registry version pages for publication status. Registry
authentication/trusted publishers must be configured before release; GitHub
repository access alone does not authorize registry publication.

## Checks

- Full Python regression run: 234 passed.
- Final architecture/contract tests: 15 passed (a separate run, not additional unique tests).
- Ruff lint and formatting checks passed for source and tests.
- `uv lock --check` and contract/package version parity passed.
- Three npm launcher tests passed.
- Installed wheel and installed npm tarball both initialized over stdio outside
  the checkout, reported version 0.3.0 and listed 11 canonical tools.
  Their schemas matched the contract; invalid mode calls returned MCP errors.
- `twine check --strict` passed for the wheel and source distribution.
- The npm tarball contains exactly the same wheel bytes as the PyPI artifact.
- `git diff --check` passed.

No tests were skipped in the full regression run.

These are software and mock/loopback checks, not physical-device certification.
See [protocol capability limits](architecture.md#protocol-capability-limits).

## Artifact SHA-256

Artifacts are local build outputs in `dist/` and `npm/`, excluded from Git.
Rebuilding can change archive hashes; these identify the verified files.

| Artifact | SHA-256 |
|---|---|
| `industriagents_ethercat_mcp-0.3.0-py3-none-any.whl` | `dca113cdea008d8359b40230ca95623a819b2367e3b9ccc2211a5fc7b48b21a5` |
| `industriagents_ethercat_mcp-0.3.0.tar.gz` | `9d458596118bf5466c532474232f62081a5def03b20d0d4ceec7f013890575d8` |
| `industriagents-ethercat-mcp-0.3.0.tgz` | `332988154c11aeb7860a0c688479da15e5670cb5cadad9c15d499d322fcbf595` |

See [release instructions](release.md) for independent registry publication.
