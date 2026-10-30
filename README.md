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
the agent for UUIDs and full replacement bodies. This one exposes **41 tools**, each an intent, built on the
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
arithmetic (`kaitencloud.usage`) and its flag builders (`kaitencloud.targeting`). A new API field reaches the
MCP by bumping the SDK. The routes the SDK has no method for yet (targeting rules, OFREP evaluation and
billing) go through its transport, with the same credential, retries and errors, as plain JSON.

## Tools

| Area | Read | Write |
| --- | --- | --- |
| Orientation | `kaiten_overview` · `kaiten_search` | |
| Customers & instances | `kaiten_get_customer` · `kaiten_get_instance` · `kaiten_list_instances` | `kaiten_save_customer` · `kaiten_provision_instance` · `kaiten_update_instance` |
| Usage | `kaiten_get_usage` | `kaiten_report_usage` |
| Catalog & Licenses | `kaiten_get_catalog` | `kaiten_save_entitlement` · `kaiten_save_license` · `kaiten_set_license_state` |
| Billing | `kaiten_get_billing` · `kaiten_get_prices` · `kaiten_get_invoices` | `kaiten_save_prices` · `kaiten_subscribe_instance` · `kaiten_cancel_subscription` · `kaiten_set_instance_addon` · `kaiten_save_voucher` · `kaiten_redeem_voucher` · `kaiten_mark_invoice_paid` |
| Feature flags | `kaiten_get_flags` · `kaiten_evaluate_flags` · `kaiten_test_rule` | `kaiten_save_flag` |
| Releases | `kaiten_get_deployments` | `kaiten_create_release` · `kaiten_deploy_release` · `kaiten_save_deployment_zone` |
| Access & audit | `kaiten_get_access` · `kaiten_get_audit_trail` | `kaiten_issue_token` · `kaiten_revoke_token` |
| Settings | `kaiten_get_metadata_fields` · `kaiten_get_connectors` | `kaiten_save_metadata_field` · `kaiten_configure_connector` |
| Deletion | | `kaiten_delete` (previews unless `confirm=true`) |

Each tool's description and input schema are the reference; `kaiten_overview` is the intended first call.
In the tools' arguments, `plan` is a License family, whose current version an instance moves to, and
`license` an exact License version.

The billing tools need billing to be on for the organization: `kaiten_get_billing` says whether it is, and
why not, and the others answer with that explanation rather than a bare 403. A voucher's code is a secret:
answers show its last four characters (`code_hint`), and the full code only in the answer of
`kaiten_save_voucher` that creates it, where the caller just gave it. Each tool names the scopes it needs in
its description:

| Billing tool | Scopes |
| --- | --- |
| `kaiten_get_billing` | `read:billing`; `read:addons` and `read:vouchers` for what is on sale; `read:instances` and `read:voucher_redemptions` for an instance |
| `kaiten_get_prices` | `read:licenses` |
| `kaiten_get_invoices` | `read:billing` |
| `kaiten_save_prices` | `write:licenses` |
| `kaiten_subscribe_instance` | `write:billing`, `read:instances`, `read:licenses` |
| `kaiten_cancel_subscription` | `write:billing`, `read:instances` |
| `kaiten_set_instance_addon` | `write:instances` |
| `kaiten_save_voucher` | `write:vouchers`; `read:licenses` to restrict it to License versions |
| `kaiten_redeem_voucher` | `write:voucher_redemptions` |
| `kaiten_mark_invoice_paid` | `write:billing` |

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
| `KAITEN_API_TOKEN` | | The token for stdio only; never used for HTTP requests |
| `KAITEN_MCP_READ_ONLY` | `false` | Register read tools only (also `--read-only`) |
| `MCP_HOST` / `MCP_PORT` | `0.0.0.0` / `8000` | HTTP listener |
| `MCP_ALLOWED_HOSTS` / `MCP_ALLOWED_ORIGINS` | | Comma-separated allowlists for the HTTP transport's Host/Origin check. Without them, the check applies when bound to localhost only |
| `KAITEN_API_TIMEOUT` | `30` | Seconds per API call |

`GET /health` reports the server's version and whether Kaiten's gateway answers.

HTTP API calls require the caller's own nonempty `Authorization: Bearer ksh_...` header. Missing,
empty or malformed credentials fail before any API call; `KAITEN_API_TOKEN` is never an HTTP fallback,
even on a single-user server. Existing headerless HTTP clients must start sending a token or use stdio.
Kaiten's gateway validates the caller's token and scopes; a 401 or 403 is never retried with a process
credential. MCP initialization and tool discovery remain public, as does `/health`. Host/Origin
allowlists protect the transport but do not authenticate callers; use TLS for remote connections.

## Development

```bash
uv sync
uv run ruff check . && uv run ruff format --check .
uv run pytest                  # unit tests: the real SDK over a mocked Kaiten
```

`tests/e2e` drives every tool but the billing ones through a real MCP client against a live Kaiten: a
subscription and its invoices can never be deleted, and the suite cleans up after itself. With
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
