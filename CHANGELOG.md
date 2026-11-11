# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project adheres to
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

AI credit tools, in preview: registered only with `KAITEN_MCP_ENABLE_AI=true`, and served by Kaiten only
where AI credits are switched on. The tools marked below need a Kaiten version that serves them (to be set).

### Added

- AI credit tools: `kaiten_get_ai_models`, `kaiten_get_ai_rules`, `kaiten_get_ai_history`,
  `kaiten_save_ai_model`, `kaiten_save_ai_rule` and `kaiten_report_ai_usage`.
- `kaiten_explain_ai_charge`: one operation's charge, rebuilt from its own pricing snapshot and usage.
  Needs the Kaiten version above.
- `kaiten_list_ai_inferences`: the organization's AI operations across instances and pools, and with
  `unpriced=true` the usage no rule priced, grouped by model and denial reason. Needs the Kaiten version
  above.
- `kaiten_get_ai_rate_card`: a pool's rules on one licence version with their level, and which rule would
  price a given model.
- `kaiten_refund_ai_inference`: gives back credits charged for one operation, under an idempotency key.
  Needs the Kaiten version above.
- `kaiten_archive_ai_rule`: archives a conversion rule, after showing which rule would price its calls
  instead.
- `kaiten_report_ai_usage` takes a commit's `vendor_cost` (needs the Kaiten version above) and says when
  an answer is a replay; `kaiten_get_ai_history` filters by authorization, model, request type and date.

### Changed

- `kaiten_report_ai_usage` previews the report until called with `confirm=true`: a reserve holds, and a
  commit charges, a customer's credits.
- `kaiten_get_ai_history` looks a transaction id up over the pool's whole history, not the last 30 days.

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
