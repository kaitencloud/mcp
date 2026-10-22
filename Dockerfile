# Kaiten MCP server.
#
#   docker run -p 8000:8000 -e KAITEN_API_BASE_URL=https://kaiten.example.com/api ghcr.io/kaitencloud/kaiten-mcp
#       Streamable HTTP at /mcp; each request authenticates with its own bearer token.
#   docker run -i --rm -e KAITEN_API_BASE_URL=... -e KAITEN_API_TOKEN=ksh_... ghcr.io/kaitencloud/kaiten-mcp stdio
#       stdio, for a local client that spawns the server.

ARG PYTHON_BASE=3.13-slim

FROM python:$PYTHON_BASE AS builder
COPY --from=ghcr.io/astral-sh/uv:0.9.1 /uv /uvx /bin/
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy
WORKDIR /app
# Dependencies first, so a source change does not reinstall them.
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev --no-install-project
COPY README.md LICENSE NOTICE ./
COPY src ./src
RUN uv sync --locked --no-dev --no-editable

FROM python:$PYTHON_BASE
WORKDIR /app
COPY --from=builder --chown=1000:1000 /app/.venv /app/.venv
USER 1000
ENV PATH="/app/.venv/bin:$PATH" PYTHONUNBUFFERED=1 MCP_PORT=8000
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=10s --start-period=10s --retries=3 \
    CMD python -c "import os, urllib.request; urllib.request.urlopen(f'http://127.0.0.1:{os.environ[\"MCP_PORT\"]}/health', timeout=5)" || exit 1
ENTRYPOINT ["kaiten-mcp"]
CMD ["http"]
