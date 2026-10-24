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
the agent for UUIDs and full replacement bodies. This one exposes **37 tools**, each an intent, built on the
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
| AI credits | `kaiten_get_ai_models` · `kaiten_get_ai_rules` · `kaiten_get_ai_history` | `kaiten_save_ai_model` · `kaiten_save_ai_rule` · `kaiten_report_ai_usage` |
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

Models are identities owned by your organization, with exact provider aliases and optional USD vendor
costs — not a synchronized provider catalogue, credential store, or inference runtime. Model costs use
string decimals (6 integer digits, 12 decimal places); conversion rates use 8 digits and 10 places;
credit caps and usage `units` use 12 digits and 6 places. Floats and exponent notation are refused.

- Define a `NUMBER_AI_CREDIT` entitlement with `kaiten_save_entitlement`. It must aggregate by `SUM`;
  its licence grant and read usage remain ordinary numeric credits. `kaiten_report_usage` accepts only
  `append` for AI credit adjustments/refunds, never `set`.
- Grant credits with `kaiten_save_license`, then configure rules with `kaiten_save_ai_rule` on an **exact
  licence version slug**, such as `premium-v2`. A rule selects an entitlement, an optional model and an
  optional request type (omitting selectors on creation means wildcard). Inspect `kaiten_get_ai_rules`
  and use its rule id to edit; an identical edit writes nothing. Creation is not retry-safe: inspect
  the rules after an uncertain create before trying again. `rates` replaces the rate map; omitted
  selectors, caps and error policy are preserved on edit. Explicit selector/cap clearing and rule
  archival are not exposed by these bounded intents.
- Editing rules changes the instances on that version immediately. Prefer a new version and configure
  its rules explicitly: `kaiten_save_license(new_version_of=...)` copies grants, **not AI rules**.
  Model and rule reads are available on read-only servers; authoring and reports are not.
- `kaiten_report_ai_usage(phase="reserve", ...)` holds estimated credits before your application sends
  a provider request. Supply a model identity or exact alias, request type and quantities. Commit the
  actual quantities with `phase="commit"` and `request_status` (`SUCCESS`, `PROVIDER_ERROR`,
  `CUSTOMER_ERROR`, or `NOT_SENT` to release an unsent request). Commit can omit model/request type to
  inherit a reservation. `usage={}` is allowed; `units` and `unitType` must be supplied together.
- Use the same `transaction_id` for reserve and commit, scoped to instance + entitlement. It is 1–128
  exact characters from `[A-Za-z0-9._:-]`; retry only the identical phase/body. The tool never sends
  `Idempotency-Key`. A changed retry is a conflict, not a new charge. Reservation TTL is validated
  against the deployment's configured maximum by Kaiten; commits refuse TTL. Reservations do not lock
  rates, and commits account actual usage even beyond limits. Inspect the returned `usage.ai` status,
  authorization, warnings, exact credits and remaining balance — a recorded commit is not authorization
  to run inference.
- `kaiten_get_ai_history` returns one bounded inference page with the API cursor, or open reservations
  with `open_reservations=true`. It preserves transaction identities and exact credit strings. Normal
  API denial/conflict codes reach the agent unchanged. No tool calls an AI provider.

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
| `KAITEN_MCP_READ_ONLY` | `false` | Register read tools only (also `--read-only`) |
| `MCP_HOST` / `MCP_PORT` | `0.0.0.0` / `8000` | HTTP listener |
| `MCP_ALLOWED_HOSTS` / `MCP_ALLOWED_ORIGINS` | | Comma-separated allowlists for the HTTP transport's Host/Origin check. Without them, the check applies when bound to localhost only |
| `KAITEN_API_TIMEOUT` | `30` | Seconds per API call |

`GET /health` reports the server's version and whether Kaiten's gateway answers.

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
