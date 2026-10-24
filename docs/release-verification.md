# 0.3.0 local release verification

Reverified on 2026-10-24 after the architecture cross-review.
Package: `industriagents-ethercat-mcp`.

These are local build and installed-artifact results, not a registry release.
No tag, commit, push or publication was performed by this verification.

## Checks

- Full server regression suite: 278 passed.
- Separate mock suite: 11 passed.
- The full suite includes the final native outcome/configuration regressions and
  the shared audit and policy-file tests; no additional focused runs are added
  to the counts above.
- Ruff lint and formatting checks passed for source and tests.
- `uv lock --check` and contract/package-version checks passed.
- Three npm launcher tests passed.
- Fresh wheel, source distribution and npm tarball built successfully.
- Both installed distribution routes initialized over stdio outside the source
  checkout and listed 11 canonical tools with the exact reviewed schemas.
  Invalid mode calls returned MCP error envelopes. Smoke tests selected the
  exact manifest-version artifact filenames.
- `twine check --strict` passed for wheel and source distribution.
- Artifact inspection confirmed current packaged sources/contracts, matching
  npm launcher bytes, and identical wheel bytes inside the npm archive.
- `git diff --check` passed.

Skipped tests: None.

The native checks use mock devices, loopback peers or fake driver APIs.
They do not certify physical hardware or add unsupported live operations.
See [architecture and protocol capability limits](architecture.md).

## Artifact SHA-256

The following artifacts are in `dist/` and `npm/`, excluded from Git.
These hashes identify the exact verified outputs; rebuilding can change them.

| Artifact | SHA-256 |
|---|---|
| `industriagents_ethercat_mcp-0.3.0-py3-none-any.whl` | `5c13e2b6264d2505412b1c1de2dc99a301f0dbccba160b619d7101dbbebe3c54` |
| `industriagents_ethercat_mcp-0.3.0.tar.gz` | `35fe35b222424c1eda7f7bb81a419b3eec666d0e37840d6e6ba0df5e012e04f8` |
| `industriagents-ethercat-mcp-0.3.0.tgz` | `cabc4155808f220e133302f777457a058a2e401a25c19a4f176f862d1eaec2dc` |

See [release instructions](release.md) for independent registry publication.
