# Kaiten MCP Server

The Kaiten MCP (Model Context Protocol) server allows AI assistants like **Claude Desktop**, **Cursor**, and **GitHub Copilot** to interact directly with the Kaiten API using natural language.

## Overview

The MCP server exposes all Kaiten API operations as MCP tools, enabling AI assistants to:
- Manage customers, licenses, and entitlements
- Configure feature flags
- Create and manage instances
- Handle service accounts and tokens
- And more...

## Prerequisites

1. **A running Kaiten API**: start one from [kaitencloud/kaiten](https://github.com/kaitencloud/kaiten) (`task up`)
2. **API Token (PAT)**: You need a Personal Access Token (PAT) starting with `ksh_` to authenticate

### Generating a PAT Token

1. Log into the Kaiten web application
2. Navigate to **Integrations** → **Service accounts**
3. Create a new Service Account (e.g., "MCP Integration")
4. Generate a new token for the service account
5. Copy the token (starts with `ksh_`) - this is shown only once!

## Local Development Setup

The `mcp` service lives behind the `mcp` Docker Compose profile and isn't
wired into a `task` yet. With the core stack already up, start it manually:

```bash
docker compose up -d --build
```

The MCP server will be available at: `http://localhost:${KAITEN_PORT}/mcp`

> **Note**: The MCP server automatically fetches the OpenAPI spec from the API at startup, so it's always in sync with the latest API version.
>
> The client's `Authorization` header is forwarded to the API, so each user's permissions are respected.

---

## Client Configuration

### Claude Desktop

Claude Desktop uses a JSON configuration file to define MCP servers.

#### Configuration File Location

| OS | Path |
|---|---|
| macOS | `~/Library/Application Support/Claude/claude_desktop_config.json` |
| Windows | `%APPDATA%\Claude\claude_desktop_config.json` |
| Linux | `~/.config/claude/claude_desktop_config.json` |

#### Configuration

Create or edit the configuration file:

```json
{
  "mcpServers": {
    "kaiten": {
      "url": "http://localhost:3080/mcp",
      "transport": "http",
      "headers": {
        "Authorization": "Bearer ksh_YOUR_TOKEN_HERE"
      }
    }
  }
}
```

> **Note**: Replace `3080` with your `KAITEN_PORT` value and `ksh_YOUR_TOKEN_HERE` with your actual PAT token.

#### Restart Claude Desktop

After saving the configuration, restart Claude Desktop completely:
- macOS: `Cmd+Q` to quit, then reopen
- Windows: Close from system tray, then reopen

#### Verify Connection

In Claude Desktop, you should see the Kaiten tools available. Try asking:
> "List all customers in Kaiten"

---

### Cursor

Cursor supports MCP servers through its settings.

#### Option 1: Via Settings UI

1. Open Cursor
2. Go to **Settings** (⌘/Ctrl + ,)
3. Search for "MCP"
4. Click **Add MCP Server**
5. Configure:
   - **Name**: `Kaiten`
   - **Type**: `HTTP` (Streamable HTTP)
   - **URL**: `http://localhost:3080/mcp`
   - **Headers**: Add `Authorization: Bearer ksh_YOUR_TOKEN_HERE`

#### Option 2: Via Configuration File

Create or edit `.cursor/mcp.json` in your project root:

```json
{
  "mcpServers": {
    "kaiten": {
      "url": "http://localhost:3080/mcp",
      "transport": "http",
      "headers": {
        "Authorization": "Bearer ksh_YOUR_TOKEN_HERE"
      }
    }
  }
}
```

Alternatively, configure globally at:
- macOS/Linux: `~/.cursor/mcp.json`
- Windows: `%USERPROFILE%\.cursor\mcp.json`

#### Verify Connection

In Cursor's chat, the Kaiten tools should appear. Try:
> "Show me all licenses in Kaiten"

---

### GitHub Copilot

GitHub Copilot's MCP support varies by IDE. Here's the configuration for VS Code with GitHub Copilot.

#### VS Code Configuration

1. Install the **GitHub Copilot** extension
2. Create or edit `.vscode/mcp.json` in your workspace:

```json
{
  "servers": {
    "kaiten": {
      "type": "http",
      "url": "http://localhost:3080/mcp",
      "headers": {
        "Authorization": "Bearer ksh_YOUR_TOKEN_HERE"
      }
    }
  }
}
```

#### Using with Copilot Chat

Once configured, you can use the Kaiten tools in Copilot Chat:
> "@mcp kaiten: list all entitlements"

---

## Environment Variables

If you prefer to use environment variables for the token (useful for CI/CD or shared configurations):

### Claude Desktop

```json
{
  "mcpServers": {
    "kaiten": {
      "url": "http://localhost:3080/mcp",
      "transport": "http",
      "headers": {
        "Authorization": "Bearer ${KAITEN_API_TOKEN}"
      }
    }
  }
}
```

Set the environment variable:
```bash
export KAITEN_API_TOKEN="ksh_your_token_here"
```

### Cursor

Cursor supports environment variable interpolation in its MCP configuration:

```json
{
  "mcpServers": {
    "kaiten": {
      "url": "http://localhost:3080/mcp",
      "transport": "http",
      "headers": {
        "Authorization": "Bearer ${env:KAITEN_API_TOKEN}"
      }
    }
  }
}
```

---

## Available Tools

The tool list is **generated at startup** from the API's OpenAPI spec, so it
always matches the running API version — don't rely on any hardcoded list; the
API reference at `/api/docs` is the authoritative catalogue. The spec currently
covers these categories:

| Category | Covers |
|---|---|
| Customers | CRUD, per-customer integrations |
| Instances | CRUD, entitlement usage metrics, audit trails |
| Licenses | CRUD, license ↔ entitlement associations |
| Entitlements & entitlement groups | CRUD, group usage |
| Feature flags | CRUD, plus OFREP evaluation |
| Releases, components, deployment zones | CRUD |
| Connectors | Settings and settings schema |
| Metadata fields | CRUD, reorder, archive/unarchive, dry-run |
| Service accounts | CRUD, token management |
| Organizations | Read |

---

## Troubleshooting

### "Connection refused" error

Ensure the Kaiten stack is running:
```bash
docker compose ps
```

Check if the MCP server is healthy:
```bash
curl http://localhost:3080/mcp/health
```

### "401 Unauthorized" error

- Verify your PAT token is correct and starts with `ksh_`
- Ensure the token hasn't expired
- Check the Authorization header format: `Bearer ksh_...`

### Tools not appearing

1. Restart your AI client (Claude Desktop, Cursor, etc.)
2. Check the MCP configuration file syntax (valid JSON)
3. Verify the URL is accessible from your machine

### Debug Mode

To see detailed logs from the MCP server:

```bash
docker compose logs -f mcp
```

---

## Security Considerations

1. **Never commit tokens**: Add configuration files with tokens to `.gitignore`
2. **Use environment variables**: For shared or CI/CD environments
3. **Rotate tokens regularly**: Create new tokens and revoke old ones periodically
4. **Limit token scope**: Use the minimum required scopes for your use case

---

## Architecture

```
┌──────────────────┐     ┌─────────────────┐
│   AI Client      │     │      Envoy      │
│ (Claude/Cursor)  │────▶│   (Validates    │
│  + ksh_* token   │     │    PAT token)   │
└──────────────────┘     └────────┬────────┘
                                  │
                                  │ /mcp/* (+ Authorization header)
                                  ▼
                         ┌─────────────────┐     ┌─────────────────┐
                         │   MCP Server    │────▶│   Kaiten API    │
                         │   (FastMCP)     │     │   (Go Server)   │
                         │                 │     │                 │
                         │ Forwards auth   │     │                 │
                         │ header to API   │     │                 │
                         └─────────────────┘     └─────────────────┘
```

1. AI client connects to MCP server via Streamable HTTP at `/mcp` with their PAT token
2. Envoy validates the client's PAT token using its `ext_authz` filter
3. MCP server captures the `Authorization` header and forwards it to API calls
4. Each user's permissions are respected - no shared service account
