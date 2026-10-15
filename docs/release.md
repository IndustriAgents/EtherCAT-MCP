# Release industriagents-ethercat-mcp

Each repository releases independently. No shared package or new repository is
required. The source distribution, wheel and npm tarball carry version 0.3.0.

1. Run `python scripts/verify_contract.py` and the repository CI checks.
2. Run `python scripts/prepare_npm.py` to build both Python distributions and copy the exact wheel into npm.
3. Run `cd npm && npm test && npm pack --ignore-scripts`.
4. Inspect the wheel and npm tarball and run a stdio initialization/tools-list
   smoke test from each installed artifact, outside the source checkout.
5. Configure a [PyPI trusted publisher](https://docs.pypi.org/trusted-publishers/)
   (pending publisher for a new project) for `IndustriAgents/EtherCAT-MCP`,
   workflow `release.yml`, environment `pypi`.
6. Configure [npm trusted publishing](https://docs.npmjs.com/trusted-publishers/)
   for the same repository/workflow, environment `npm`. A first publication may
   require the maintainer's authenticated npm session before this relationship
   can be configured. The intended package is `industriagents-ethercat-mcp`.
7. Publish tag `v0.3.0` only after checks pass. The workflow builds once and
   publishes those artifacts to both registries; it never rebuilds in the
   publication jobs. A manual workflow run on a branch builds without publishing.

Registry access is separate from GitHub repository access. A successful local
build is not a registry release. If one registry fails, verify its version
before retrying; never overwrite a published version. The npm artifact bundles
the wheel, so partial registry availability cannot silently select a different
Python implementation.

Local verification results and artifact hashes are recorded in
[release-verification.md](release-verification.md).

## Retry an existing release after configuring registry access

An explicit manual release run checks out the existing immutable tag, validates
its version, rebuilds and tests it, then publishes the resulting artifacts. It
uses the current workflow so publication-only fixes do not require moving a tag:

```sh
gh workflow run release.yml --ref main -f release_tag=v0.3.0
```

A manual run without `release_tag` still builds without publishing. If either
registry already contains 0.3.0, rerun only the failed publishing job from the
appropriate run instead of requesting a new full publication. For npm's first
publication, an authenticated maintainer may need to publish the verified
`./npm/*.tgz` artifact before configuring trusted publishing.
