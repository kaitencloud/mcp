# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project adheres to
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- `kaiten_save_plan_settings`, a billing tool for a plan's own settings: whether the organization's public
  catalogue lists it, the FREE plan its instances fall back to when a subscription ends, what a trial without
  a payment method becomes at its end, and how many days before a trial's end its reminder fires. Its answer
  names the version the public catalogue offers, or why it offers none. The server goes from 44 to 45 tools.
- `kaiten_save_license` sets how a version is sold: `pricing` (FREE, PAID or CUSTOM),
  `requires_payment_method`, `trial_days` and `self_serve_cta_url`. `kaiten_get_catalog` shows them on each
  version, with each plan's public listing and fallback plan, and with a plan, all of its settings.

### Changed

- `kaiten_set_license_state` says when moving a public plan's default version changes what its public
  catalogue offers, or takes the plan out of it.

### Fixed

- A version made with `kaiten_save_license(new_version_of=...)` is sold as the version it comes from. It was
  created CUSTOM, Kaiten's default: made the default, it stopped a self-serve plan selling at checkout, and
  Kaiten refused it outright on a plan another plan falls back to (`CreateLicense.FallbackMustBeFree`).

## [0.3.0] - 2026-11-02

### Added

- Ten billing tools, for an organization where billing is on: `kaiten_get_billing`, `kaiten_get_prices`
  (with the invoice a subscription would issue), `kaiten_get_invoices`, `kaiten_save_prices`,
  `kaiten_subscribe_instance`, `kaiten_cancel_subscription`, `kaiten_set_instance_addon`, `kaiten_save_voucher`,
  `kaiten_redeem_voucher` and `kaiten_mark_invoice_paid`. Where billing is off, they say why, from Kaiten's
  billing capabilities. A voucher's code shows by its last four characters, except in the answer that creates it.
- Three webhook tools for Kaiten Cloud: `kaiten_get_webhooks`, which also searches the catalogue of the events
  Kaiten publishes, `kaiten_save_webhook` and `kaiten_delete_webhook`. The catalogue is read from the SDK's
  generated event models. A webhook URL shows by its scheme and host only, and a new endpoint's signing secret
  only in the answer that creates it.
- The server goes from 31 to 44 tools, and read-only mode from 15 to 19.

### Changed

- The server requires `kaitencloud` 1.1.0, whose models follow Kaiten 26.10.8: use it with Kaiten 26.10.8 or
  later. The scopes `kaiten_issue_token` takes come from the SDK's `Scope`, which now lists billing's.

### Security

- Over Streamable HTTP, each request must bring its own `Authorization: Bearer ksh_...` header.
  `KAITEN_API_TOKEN` is never an HTTP fallback, even on a single-user server: a client that sent no
  header is now refused, and must send its own token or use stdio. stdio is unchanged.
- Critical TLS and JWT dependencies are updated.

### Fixed

- Usage is set against the limit Kaiten enforces on the instance: the License's grant with its
  add-ons, voucher boosts and entitlement exception applied, rather than the License's grant alone.
  `kaiten_get_usage`, `kaiten_get_instance` and the views built on them read it, and
  `kaiten_report_usage` no longer refuses a report on an entitlement held through an add-on or an
  exception.
- Kaiten's refusals reach the agent whole (status, code, detail, error id and each field at fault),
  and the server logs them on one line instead of a traceback.
- `kaiten_list_instances` finds a lifecycle stage whatever its case.

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

[0.3.0]: https://github.com/kaitencloud/mcp/releases/tag/v0.3.0
[0.2.0]: https://github.com/kaitencloud/mcp/releases/tag/v0.2.0
