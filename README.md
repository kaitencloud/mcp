# Kaiten MCP Server

An [MCP](https://modelcontextprotocol.io) server that lets an AI agent (Claude, Cursor, Copilot, any MCP
client) run Kaiten: onboard customers, provision instances, shape Licenses and entitlements, meter usage,
target feature flags and ship releases — in Kaiten's own vocabulary, not in HTTP verbs.

```text
"Which customers are close to a limit?"            → kaiten_get_usage(min_percent=80)
"Move Ninja Osaka to Premium and renew it for a    → kaiten_update_instance(instance="ninja-osaka-prod",
 year"                                                 plan="premium", extend_license_days=365)
"Give Starter 20 more menu items, without          → kaiten_save_license(new_version_of="starter",
 touching existing customers"                          grants={"menu-items": 25})
"Why doesn't Sakura see the analytics dashboard?"  → kaiten_evaluate_flags(instance="sakura-tokyo-prod")
```

## Design

The previous server turned every OpenAPI operation into a tool: 105 tools, one per endpoint, each asking
the agent for UUIDs and full replacement bodies. This one exposes **31 tools by default**, plus eleven experimental AI-credit intents when explicitly enabled, built on the
rules in Kaiten's product vision for agent-readiness:

- **Tools express an intent, not a CRUD mirror.** `kaiten_provision_instance` resolves a License family to its
  current version, finds the zone, creates the customer if asked and sets the license term; the agent never
  handles an id. `kaiten_get_customer` answers "how is this customer doing" in one call.
- **Reads return slices, never the whole catalog.** Resources are projected onto the fields that carry
  meaning, named by slug, with usage already set against its limit (`used`, `limit`, `percent`, `state`).
- **Writes merge, and report truthfully.** Only what an agent passes changes. Every write answers with a
  `status` — `created`, `updated`, `unchanged`, `recorded`, `rejected`, `preview` or `deleted` — and the
  exact `changes`, computed from what Kaiten holds afterwards. A call that changes nothing says `unchanged`
  and writes nothing; a refused usage report or a broken targeting rule says `rejected` and writes nothing.
- **Identifiers are constrained and stable.** Every tool takes slugs, validated before any call.
- **Trace before change.** Writes that reach other resources say so (`affected_instances` when a license
  version is edited in place, the zone's instances when a release is deployed), and `kaiten_delete` previews
  its blast radius until called with `confirm=true`.
- **An agent's blast radius is its principal's.** Each request carries the caller's own Kaiten token; the
  server has no credential of its own. `KAITEN_MCP_READ_ONLY=true` registers the read tools only.

There is no API contract in this repository. Every call goes through the official
[Python SDK](https://github.com/kaitencloud/sdk-python) (`kaitencloud`), generated from Kaiten's OpenAPI
document: its methods, its models, its enums (which become the tools' input schemas), its enforcement
arithmetic (`kaitencloud.usage`) and its flag builders (`kaitencloud.targeting`). AI control-plane endpoints not yet wrapped by the installed SDK use its authenticated transport,
like targeting authoring; their intent inputs follow the Kaiten OpenAPI contract without editing generated code.

## Tools

| Area | Read | Write |
| --- | --- | --- |
| Orientation | `kaiten_overview` · `kaiten_search` | |
| Customers & instances | `kaiten_get_customer` · `kaiten_get_instance` · `kaiten_list_instances` | `kaiten_save_customer` · `kaiten_provision_instance` · `kaiten_update_instance` |
| Usage | `kaiten_get_usage` | `kaiten_report_usage` |
| AI credits (opt-in only, preview) | `kaiten_get_ai_models` · `kaiten_get_ai_rules` · `kaiten_get_ai_rate_card` · `kaiten_get_ai_history` · `kaiten_list_ai_inferences` · `kaiten_explain_ai_charge` | `kaiten_save_ai_model` · `kaiten_save_ai_rule` · `kaiten_archive_ai_rule` · `kaiten_report_ai_usage` · `kaiten_refund_ai_inference` (the last three preview unless `confirm=true`) |
| Catalog & Licenses | `kaiten_get_catalog` | `kaiten_save_entitlement` · `kaiten_save_license` · `kaiten_set_license_state` |
| Feature flags | `kaiten_get_flags` · `kaiten_evaluate_flags` · `kaiten_test_rule` | `kaiten_save_flag` |
| Releases | `kaiten_get_deployments` | `kaiten_create_release` · `kaiten_deploy_release` · `kaiten_save_deployment_zone` |
| Access & audit | `kaiten_get_access` · `kaiten_get_audit_trail` | `kaiten_issue_token` · `kaiten_revoke_token` |
| Settings | `kaiten_get_metadata_fields` · `kaiten_get_connectors` | `kaiten_save_metadata_field` · `kaiten_configure_connector` |
| Deletion | | `kaiten_delete` (previews unless `confirm=true`) |

Each tool's description and input schema are the reference; `kaiten_overview` is the intended first call.
In the tools' arguments, `plan` is a License family, whose current version an instance moves to, and
`license` an exact License version.

## AI credit control plane

AI credits are in **preview**: Kaiten serves them only where its platform flag is on, and the charge
explanations, the organization listing, refunds, vendor costs on commits and rule authors need a Kaiten
version that serves them (version to be set). The AI tools are **off by default** in this server. Set
`KAITEN_MCP_ENABLE_AI=true` before starting the process to register the experimental module: six AI reads
in read-only mode, all eleven intents in write mode. Unset, false and malformed values deny; only `true`
(case-insensitive, surrounding whitespace ignored) opts in. Restart the process to change this fixed
policy. Without it, the initialization instructions carry no AI-credit guidance, and calls to unregistered
AI names make no API requests.

This is local opt-in, **not authorization**. The API independently authenticates/scopes the caller and
requires its deployment AI policy; a server `403 AIUsage.Disabled` remains authoritative, even with local
opt-in or caller metadata. No tool changes that policy or calls a provider. Plan a quiesce/drain before
server disable: disabling during inference may leave settlement pending; retain the transaction identity.

The switch only adds the AI tools. Whatever it says, the generic tools keep what they have done since
0.2.0: `kaiten_get_catalog`, `kaiten_search`, `kaiten_get_usage` and the customer and instance views list
`NUMBER_AI_CREDIT` entitlements, `kaiten_save_entitlement` and `kaiten_save_license` create and grant them,
and `kaiten_report_usage` appends to them. A stricter projection, which omits AI credit entitlements from
the generic tools and refuses to change them before any write, exists for code that builds the server
itself (`kaiten_mcp.server.build(hide_ai_entitlements=True)`); no environment variable turns it on. It
classifies rows by contract types only, never by config or metadata keys; targeting inspection uses
Core's original-type metadata (`entitlementType` on known schema entries, `entitlementTypes` beside
rehearsal facts) and omits unclassified entries from older servers; immutable audit evidence stays
readable unchanged.

The installed SDK's private authenticated HTTP bridge stays in use until a coherent experimental SDK
namespace/contract is available. No SDK pin change or eager experimental namespace import is required.

Models are identities owned by your organization, with exact provider aliases and optional USD vendor
costs — not a synchronized provider catalogue, credential store, or inference runtime. Model costs use
string decimals (6 integer digits, 12 decimal places); conversion rates use 8 digits and 10 places;
credit caps, refunds and usage `units` use 12 digits and 6 places; a commit's `vendor_cost` 12 digits and
12 places. Floats and exponent notation are refused.

- Define a `NUMBER_AI_CREDIT` entitlement with `kaiten_save_entitlement`. It must aggregate by `SUM`;
  its licence grant and read usage remain ordinary numeric credits. `kaiten_report_usage` accepts only
  `append` for AI credit adjustments, never `set`; a wrong charge is corrected with
  `kaiten_refund_ai_inference`.
- Grant credits with `kaiten_save_license`, then configure rules with `kaiten_save_ai_rule` on an **exact
  licence version slug**, such as `premium-v2`. A rule selects an entitlement, an optional model and an
  optional request type (omitting selectors on creation means wildcard). Inspect `kaiten_get_ai_rules`
  (which also says who created and last changed each rule, and whether the licence or a compatible add-on
  grants the pool) and use its rule id to edit; an identical edit writes nothing. Creation is not
  retry-safe: inspect the rules after an uncertain create before trying again. `rates` replaces the rate
  map; omitted selectors, caps and error policy are preserved on edit. Explicit selector/cap clearing is
  not exposed. `kaiten_archive_ai_rule` archives a rule: without `confirm` it shows the rule and which
  rule would price its calls instead, or none; an archived rule cannot be edited or restored.
- Editing rules changes the instances on that version immediately. Prefer a new version and configure
  its rules explicitly: `kaiten_save_license(new_version_of=...)` copies grants, **not AI rules**.
  Model and rule reads are available on read-only servers; authoring, reports and refunds are not.
- `kaiten_report_ai_usage` moves a customer's credits, so it only previews the report it would send
  until it is called again with `confirm=true`, for reserve and commit alike. `phase="reserve"` holds
  estimated credits before your application sends a provider request. Supply a model identity or exact
  alias, request type and quantities. Commit the actual quantities with `phase="commit"` and
  `request_status` (`SUCCESS`, `PROVIDER_ERROR`, `CUSTOMER_ERROR`, or `NOT_SENT` to release an unsent
  request). Commit can omit model/request type to inherit a reservation. `usage={}` is allowed; `units`
  and `unitType` must be supplied together. A commit of a sent request may carry `vendor_cost`, what the
  provider charged in USD as a decimal string: evidence beside the cost Kaiten computes from the model's
  catalogue costs, never a change to the credits charged. A release for a transaction that never
  reserved answers `RELEASED` with the warning `NO_RESERVATION` and closes that transaction id: a later
  reserve or commit under it is a conflict.
- Use the same `transaction_id` for reserve and commit, scoped to instance + entitlement. It is 1–128
  exact characters from `[A-Za-z0-9._:-]`; retry only the identical phase/body, `vendor_cost` included
  byte for byte (`0.01` and `0.010` are different bodies). The tool never sends `Idempotency-Key` on a
  report. A changed retry is a conflict, not a new charge; an identical one answers `replayed: true`.
  Reservation TTL is validated against the deployment's configured maximum by Kaiten; commits refuse
  TTL. Reservations do not lock rates, and commits account actual usage even beyond limits. Inspect the
  returned `usage.ai` status, authorization, the rule that priced it (`rule {id, specificity}`), warnings,
  exact credits and remaining balance — a recorded commit is not authorization to run inference.
- `kaiten_get_ai_history` returns one bounded inference page of a pool with the API cursor, or open
  reservations with `open_reservations=true`; `kaiten_list_ai_inferences` reads the whole organization,
  each row naming its instance and pool. Rows carry, where the server keeps them, the pricing snapshot
  that charged them, the licence, warnings, `reportSeq`, `vendorCost`, `refunds` and `credits.refunded`.
  Filters are `authorized`, `model_slug`, `model` (as sent), `request_type`, `status` and `since`/`until`
  (the last 30 days by default; a `transaction_id` alone searches the whole history). Members and warning
  codes a server adds later are passed through untouched.
- `kaiten_list_ai_inferences(unpriced=true)` groups the usage no rule priced (`authorized=false`) by model
  as sent and denial reason (`UNKNOWN_MODEL`, `MODEL_ARCHIVED`, `NO_RULE`). The grouping is done by this
  server from at most `scan_limit` operations, and the answer says when more exist.
- `kaiten_explain_ai_charge` explains one operation from its own record: the rule snapshot, the formula
  recomputed from the snapshot and the stored usage, the error policy and the clamp, set against Kaiten's
  figures, the counter before and after, vendor cost, warnings with their meaning, refunds and what is left
  to refund. It never reads today's rules; an operation recorded without a snapshot is said to have none.
- `kaiten_get_ai_rate_card` lists a pool's active rules on one licence version with their ladder level,
  and for a model (catalogue slug or exact external id) says which rule would price each request type. It
  is computed by this server from the rules and models read now, with Kaiten's ladder: the model's own
  rule for the type, then its own rule for every type, then any model for the type, then any model for
  every type. Unknown and archived models are never priced, not even by a wildcard. Kaiten decides at
  report time.
- `kaiten_refund_ai_inference` gives back credits charged for one committed operation, in full or in
  part, under an `idempotency_key` the agent chooses once and reuses on every retry. Without `confirm` it
  shows the charge, what was refunded, what is left, and what Kaiten would refuse. The refund lands in the
  usage window open now, as a negative report never refused by the pool's limit; the answer says what is
  left to refund. Normal API denial/conflict codes reach the agent unchanged. No tool calls an AI provider.

### Configure AI pricing

This repository ships no skills or prompts, so the workflow lives here and in the tools' descriptions:

1. **Models.** `kaiten_save_ai_model` once per model your application calls, with the exact identifiers
   it sends as `model` in `external_ids`, and optionally USD vendor costs. A model the catalogue does not
   know is never priced.
2. **Pool.** `kaiten_save_entitlement(type="NUMBER_AI_CREDIT", aggregation="SUM")`.
3. **Grants.** `kaiten_save_license` grants the pool on each licence version; prefer a new version
   (`new_version_of=...`) to editing a published one. Rules are not copied to a new version: do step 4
   for it again.
4. **Rules.** `kaiten_save_ai_rule` per licence version, pool and selector. A rule with a model and a
   request type is the most specific; leaving either out makes a wildcard. **A wildcard prices future
   models too**: every model added to the catalogue later, and every model that is not archived and has no
   rule of its own, is charged at the wildcard's rates from its first call. Use one only when that is what
   you want, and give expensive models their own rule.
5. **Check.** `kaiten_get_ai_rate_card(license=..., entitlement=..., model=...)` says which rule would
   apply to each request type, without holding any credit. Never reserve to test a price: a reserve is a
   real hold on the customer's pool.
6. **Watch.** `kaiten_list_ai_inferences(unpriced=true)` shows which models customers call that no rule
   prices; `kaiten_explain_ai_charge` explains any single charge.
7. **Correct.** A release (`NOT_SENT` commit) never charges, and a release for a transaction that never
   reserved closes its id for good. A wrong charge is corrected with `kaiten_refund_ai_inference`, which
   stays tied to the charge it corrects and gives back at most what was charged, not with a negative
   `kaiten_report_usage`. Past usage is never repriced by a rule change.

Example rule input: `license="premium-v2", entitlement="ai-credits", model="assistant",
request_type="COMPLETION", rates={"perInputToken": "0.000001", "perOutputToken": "0.000002"}`.
AI `usage` accepts `inputTokens`, `outputTokens`, `cacheReadInputTokens`, `cacheWriteInputTokens`,
`durationMs` (non-negative int64 integers), and `units`/`unitType` (`image`, `second`, `frame`).
Rates additionally accept `perCachedInputToken`, `perCacheWriteToken`, `perRequest`, `perSecond` and
`perUnit`; vendor costs accept those except `perSecond`.

## Running it

It speaks both standard MCP transports:

- **stdio**, for a client that spawns the server on your machine. The token comes from `KAITEN_API_TOKEN`.
- **Streamable HTTP** at `/mcp`, for a shared or remote server. Each request authenticates with its own
  `Authorization: Bearer ksh_...` header, which the server forwards to Kaiten untouched.

```bash
# from PyPI, with uv
KAITEN_API_BASE_URL=http://localhost:6060/api KAITEN_API_TOKEN=ksh_... uvx kaitencloud-mcp stdio
uvx kaitencloud-mcp http --port 8000

# with Docker
docker run -i --rm -e KAITEN_API_BASE_URL=... -e KAITEN_API_TOKEN=ksh_... ghcr.io/kaitencloud/kaiten-mcp:0.2 stdio
docker run -p 8000:8000 -e KAITEN_API_BASE_URL=... ghcr.io/kaitencloud/kaiten-mcp:0.2   # http, the default

# from a checkout
KAITEN_API_BASE_URL=http://localhost:6060/api KAITEN_API_TOKEN=ksh_... uv run kaiten-mcp stdio
docker build -t kaiten-mcp .
```

The package is [`kaitencloud-mcp`](https://pypi.org/project/kaitencloud-mcp/) and the image
`ghcr.io/kaitencloud/kaiten-mcp`. `kaiten-mcp` on PyPI is an unrelated project.

[MCP.md](./MCP.md) has the client configurations (Claude Code, Claude Desktop, Cursor, VS Code) and how to
get a token.

| Variable | Default | |
| --- | --- | --- |
| `KAITEN_API_BASE_URL` | `http://localhost:6060/api` | Kaiten's **gateway**: it is what accepts a `ksh_` token. Not the API container |
| `KAITEN_API_TOKEN` | | The token for stdio, or for an HTTP request that brings none |
| `KAITEN_MCP_ENABLE_AI` | `false` | Explicit `true` registers experimental AI intents; not server authorization |
| `KAITEN_MCP_READ_ONLY` | `false` | Register read tools only (also `--read-only`) |
| `MCP_HOST` / `MCP_PORT` | `0.0.0.0` / `8000` | HTTP listener |
| `MCP_ALLOWED_HOSTS` / `MCP_ALLOWED_ORIGINS` | | Comma-separated allowlists for the HTTP transport's Host/Origin check. Without them, the check applies when bound to localhost only |
| `KAITEN_API_TIMEOUT` | `30` | Seconds per API call |

`GET /health` reports the server's version and whether Kaiten's gateway answers.

For shared HTTP deployments, leave `KAITEN_API_TOKEN` unset: an absent Authorization header otherwise
uses that process credential (the existing single-user/stdio fallback). A supplied caller credential
always takes precedence; denial never retries as the process token.

## Development

```bash
uv sync
uv run ruff check . && uv run ruff format --check .
uv run pytest                  # unit tests: the real SDK over a mocked Kaiten
```

`tests/e2e` drives every tool through a real MCP client against a live Kaiten. With
[Kaiten](https://github.com/kaitencloud/kaiten)'s own stack and its demo dataset (`task quickstart`, which
seeds the Kaiten Sushi Shop), and a token with `read:*` and `write:*` on that organization:

```bash
KAITEN_API_BASE_URL=http://localhost:6060/api KAITEN_API_TOKEN=ksh_... uv run pytest -m e2e
```

It creates everything it needs under a run-unique suffix and deletes it afterwards, except one service
account (`e2e-mcp`): Kaiten has no way to delete one.

## License

The Kaiten MCP server is open source and licensed under the
[Apache License, Version 2.0](./LICENSE).

By contributing, you agree to certify your contribution under the
[Developer Certificate of Origin 1.1](./DCO.md).

The Kaiten name and logos are not licensed under Apache-2.0. See the
[Kaiten trademark policy](https://github.com/kaitencloud/kaiten/blob/main/TRADEMARKS.md).
