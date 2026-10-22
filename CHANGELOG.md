# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project adheres to
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.2.0] - 2026-10-22

The first public release of the Kaiten MCP server. It replaces the server that turned every operation
of the Kaiten API into a tool of its own, which was never released.

### Added

- 31 intent-level tools over the Kaiten API, built on the official Python SDK (`kaitencloud`):
  orientation and search, customers and instances, usage, the catalog and Licenses, feature flags,
  releases and deployment zones, access and audit, settings, and a deletion that previews its blast
  radius until confirmed.
- The stdio transport, with the token in `KAITEN_API_TOKEN`, and Streamable HTTP at `/mcp`, where
  each request brings its own `Authorization: Bearer ksh_...` header.
- A read-only mode (`KAITEN_MCP_READ_ONLY=true` or `--read-only`) that registers the 15 read tools
  only.
- `GET /health`: the server's version, and whether Kaiten's gateway answers.
- The package on PyPI, [`kaitencloud-mcp`](https://pypi.org/project/kaitencloud-mcp/), and the image
  `ghcr.io/kaitencloud/kaiten-mcp`.

[0.2.0]: https://github.com/kaitencloud/mcp/releases/tag/v0.2.0
