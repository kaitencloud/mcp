# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project adheres to
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

AI credit tools, in preview: registered only with `KAITEN_MCP_ENABLE_AI=true`, and served by Kaiten only
where AI credits are switched on. The tools marked below need **Kaiten 26.11.3 or later**, the first
release carrying [kaitencloud/kaiten#98](https://github.com/kaitencloud/kaiten/pull/98).

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
- `kaiten_update_instance` sends only the metadata keys passed (`null` removes one), for Kaiten to merge
  into what it holds with `PATCH /instances/{slug}`, so a change another caller makes to other keys
  meanwhile is kept. A Kaiten whose `PATCH` takes no metadata refuses the member before applying
  anything; the tool then writes the merged metadata back whole with a `PUT`, as before, and says so in
  its answer.
- `kaiten_save_flag` checks a beta's prerequisites before it writes one. A flag whose metadata `stage` is
  `private_beta`, `public_beta` or `open_beta`, or whose rules read `betaAccess`, `betaOptIn` or
  `betaOptOut` on the instance, needs those lists declared as instance metadata fields that accept its
  slug: otherwise its rules can never match. When one is missing or refuses the slug, nothing is written
  and the answer carries, under `beta.fix`, the `kaiten_save_metadata_field` calls that prepare them. The
  check runs only when a call makes the flag a beta, moves its stage or changes which lists its rules
  read, so turning a beta off is never held back. `kaiten_get_flags` shows the same state for one flag.

## [0.4.0] - 2026-11-06

### Added

- `kaiten_save_plan_settings`, a billing tool for a plan's own settings: whether the organization's public
  catalogue lists it, the FREE plan its instances fall back to when a subscription ends, what a trial without
  a payment method becomes at its end, and how many days before a trial's end its reminder fires. Its answer
  names the version the public catalogue offers, or why it offers none, and warns of a public plan with a
  PAID version and no fallback plan, as the console does. `kaiten_get_catalog` says whether each public plan
  is listed. The server goes from 44 to 45 tools.
- `kaiten_save_license` sets how a version is sold: `pricing` (FREE, PAID or CUSTOM),
  `requires_payment_method`, `trial_days` and `self_serve_cta_url`. `kaiten_get_catalog` shows them on each
  version, with each plan's public listing and fallback plan, and with a plan, all of its settings.
- `kaiten_report_usage` takes an `idempotency_key`, sent as the report's `Idempotency-Key`. A report repeated
  under the same key, behavior and amount counts nothing again, and answers `unchanged` and `replayed`; a key
  already spent on a report of another behavior or amount answers `rejected`. The answer carries the
  `transaction_id` Kaiten recorded and `replayed`, and says when Kaiten did not store metadata over 4 KiB.
- `kaiten_get_usage_reports`, a read tool: the reports Kaiten accepted for an instance's entitlement, newest
  first, or the one sent under an `idempotency_key`. The server goes from 45 to 46 tools, and read-only mode
  from 19 to 20.
- Usage shows when its window started, `period_start`, next to `resets_at`, in `kaiten_get_instance`,
  `kaiten_get_usage` and `kaiten_report_usage`'s answer: a BILLING_PERIOD window can be checked against the
  subscription's period.
- `kaiten_save_metadata_field` takes a `position` among the resource type's active fields, 0 first. It
  rewrites their order only when a field is not at its place, which also separates the fields an archive and
  a re-create left at the same position, and `changes.order` says which moved.
- An unlimited NUMBER grant shows the values Kaiten stores next to `"unlimited"`: `raw_value` and
  `overage_percent`, -1 and -1, in `kaiten_get_catalog` and `kaiten_save_license`'s answers. Usage measured
  against an unlimited limit shows `raw_limit` and `overage_percent`.
- `kaiten_get_customer` shows a customer's `billing_email`, where invoices sent to it go, and
  `kaiten_save_customer` sets it: Stripe needs one to send an invoice (`SEND_INVOICE`). Left out, it is kept,
  and `""` removes it.

### Changed

- `kaiten_set_license_state` says when moving a public plan's default version changes what its public
  catalogue offers, or takes the plan out of it.
- The server's instructions: a usage report is safe to repeat only with the same `idempotency_key`; without
  one, never repeat a report that answered.

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

[Unreleased]: https://github.com/kaitencloud/mcp/compare/v0.4.0...HEAD
[0.4.0]: https://github.com/kaitencloud/mcp/releases/tag/v0.4.0
[0.3.0]: https://github.com/kaitencloud/mcp/releases/tag/v0.3.0
[0.2.0]: https://github.com/kaitencloud/mcp/releases/tag/v0.2.0
