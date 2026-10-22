# Connecting a client

Two ways to reach the server, and every client below supports at least one:

| | stdio | Streamable HTTP |
| --- | --- | --- |
| Who runs the server | your client spawns it, on your machine | a deployment, behind Kaiten's gateway |
| Where the token goes | `KAITEN_API_TOKEN` in the server's environment | an `Authorization: Bearer ksh_...` header on every request |
| Address | — | `/mcp` on the server itself: `http://localhost:8000/mcp` with this repo's `compose.yml` |

Kaiten's gateway (port 6060) has no `/mcp` route: run the server next to it and call the server directly.

## Getting a token

A token belongs to a service account in your organization. In the console: **Integrations → Service
accounts**, create one (e.g. "MCP"), then issue a token and copy it — it starts with `ksh_` and is shown
once. Grant what the agent should be able to do: `read:*` for an agent that only looks, `read:*` and
`write:*` for one that operates. An agent that already has access can issue one with `kaiten_issue_token`.

## Claude Code

```bash
# remote (Streamable HTTP)
claude mcp add --transport http kaiten http://localhost:8000/mcp \
  --header "Authorization: Bearer ksh_..."

# local (stdio)
claude mcp add kaiten \
  -e KAITEN_API_BASE_URL=http://localhost:6060/api -e KAITEN_API_TOKEN=ksh_... \
  -- uvx kaitencloud-mcp stdio
```

## Claude Desktop

`claude_desktop_config.json` (macOS: `~/Library/Application Support/Claude/`, Windows: `%APPDATA%\Claude\`)
starts local servers:

```json
{
  "mcpServers": {
    "kaiten": {
      "command": "uvx",
      "args": ["kaitencloud-mcp", "stdio"],
      "env": {
        "KAITEN_API_BASE_URL": "http://localhost:6060/api",
        "KAITEN_API_TOKEN": "ksh_..."
      }
    }
  }
}
```

With Docker instead of uv: `"command": "docker"`, `"args": ["run", "-i", "--rm", "-e", "KAITEN_API_BASE_URL",
"-e", "KAITEN_API_TOKEN", "ghcr.io/kaitencloud/kaiten-mcp:0.2", "stdio"]`, with the same `env`. A remote server is added from
**Settings → Connectors** instead.

## Cursor

`.cursor/mcp.json` in a project, or `~/.cursor/mcp.json`:

```json
{
  "mcpServers": {
    "kaiten": {
      "url": "http://localhost:8000/mcp",
      "headers": { "Authorization": "Bearer ${env:KAITEN_API_TOKEN}" }
    }
  }
}
```

## VS Code (Copilot)

`.vscode/mcp.json`:

```json
{
  "inputs": [{ "id": "kaiten-token", "type": "promptString", "description": "Kaiten ksh_ token", "password": true }],
  "servers": {
    "kaiten": {
      "type": "http",
      "url": "http://localhost:8000/mcp",
      "headers": { "Authorization": "Bearer ${input:kaiten-token}" }
    }
  }
}
```

## Running the server for a local Kaiten

`docker compose up -d` here runs the server on its own, on `localhost:8000/mcp`, pointed at
`KAITEN_API_BASE_URL` (by default a gateway on the host's port 6060):

```bash
docker compose up -d
curl http://localhost:8000/health
```

## Troubleshooting

- **401 from `/mcp`** — the gateway refused the credential: it must be `Bearer ksh_...`, active, and not
  expired.
- **A tool answers `Kaiten API 403 Auth.MissingScope`** — the token lacks the scope that operation needs;
  issue one with more.
- **Every call fails to connect** — `KAITEN_API_BASE_URL` must be the gateway as seen *from the server*
  (inside Docker, not `localhost`). `GET /health` says whether it answers.
- **Tools changed but the client shows the old ones** — restart the client; most cache the tool list.

## Security

- The server holds no credential of its own: each request acts as its caller's token, and nothing more.
- Give an agent the least it needs. A read-only server (`KAITEN_MCP_READ_ONLY=true`) cannot even attempt a
  write, whatever its token allows.
- Never commit a client configuration that contains a token; prefer environment variables or a prompt.
