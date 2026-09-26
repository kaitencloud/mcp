# Kaiten MCP Server

A FastMCP server that exposes the Kaiten API as MCP tools for AI assistants like Claude Desktop, Cursor, and GitHub Copilot.

## Overview

This server fetches the Kaiten OpenAPI specification directly from the API at startup and converts all endpoints into MCP tools, enabling natural language interaction with the Kaiten platform.

The spec is always in sync with the API - no manual updates needed.

## Usage

See [MCP.md](./MCP.md) for setup instructions.

## Development

```bash
# Install dependencies
uv sync

# Run the server (SSE transport)
uv run python server.py

# Run the server (stdio transport for local testing)
uv run python server.py --stdio
```

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `KAITEN_API_BASE_URL` | `http://api:3080/api` | Base URL for authenticated Kaiten API requests |
| `KAITEN_API_OPENAPI_URL` | `http://api:3080/api/openapi.json` | URL used at startup to fetch the OpenAPI spec |

## How It Works

1. On startup, fetches `/api/openapi.json` from the API
2. Converts all API endpoints to MCP tools
3. Captures the client's `Authorization` header via middleware
4. Forwards that header to all API calls (per-request auth)
