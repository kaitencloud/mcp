# Copyright 2026 KAITEN INC
# SPDX-License-Identifier: Apache-2.0

"""kaiten-mcp: run the server over stdio (local clients) or Streamable HTTP (remote clients)."""

from __future__ import annotations

import argparse

import uvicorn

from .config import settings
from .server import build, http_app


def main() -> None:
    parser = argparse.ArgumentParser(prog="kaiten-mcp", description="Kaiten MCP server")
    parser.add_argument(
        "transport",
        nargs="?",
        choices=["stdio", "http"],
        default="stdio",
        help="stdio for a local client that spawns the server (uses KAITEN_API_TOKEN); "
        "http to serve Streamable HTTP at /mcp, authenticating each request by its own bearer token",
    )
    parser.add_argument("--host", default=settings.host)
    parser.add_argument("--port", type=int, default=settings.port)
    parser.add_argument(
        "--read-only",
        action="store_true",
        help="register read tools only (or KAITEN_MCP_READ_ONLY=true)",
    )
    args = parser.parse_args()

    mcp = build(writes=False if args.read_only else None)
    if args.transport == "stdio":
        mcp.run(transport="stdio", show_banner=False)
    else:
        uvicorn.run(http_app(mcp), host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
