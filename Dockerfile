# Kaiten MCP Server Dockerfile
# Multi-stage build with uv for fast, reproducible builds

ARG PYTHON_BASE=3.13-slim

# =============================================================================
# Builder stage: Install dependencies and compile bytecode
# =============================================================================
FROM python:$PYTHON_BASE AS builder

# Install uv
COPY --from=ghcr.io/astral-sh/uv:0.9.1 /uv /uvx /bin/

WORKDIR /app

# Install dependencies (not the project itself - we run server.py directly)
COPY pyproject.toml uv.lock* ./
RUN uv sync --locked --compile-bytecode --no-install-project --no-dev || \
    uv sync --compile-bytecode --no-install-project --no-dev

# =============================================================================
# Runtime stage: Minimal image with only what's needed to run
# =============================================================================
FROM python:$PYTHON_BASE

WORKDIR /app

# Copy virtual environment from builder
COPY --from=builder --chown=1000:1000 /app/.venv /app/.venv

# Copy application files
COPY --chown=1000:1000 server.py ./server.py

# Run as non-root user for security
USER 1000

# Environment configuration
ENV PATH="/app/.venv/bin:$PATH"
ENV PYTHONUNBUFFERED=1

# Expose the MCP server port
EXPOSE 8000

# Health check using the /health endpoint
HEALTHCHECK --interval=30s --timeout=10s --start-period=10s --retries=3 \
    CMD python -c "import httpx; httpx.get('http://localhost:8000/health', timeout=5).raise_for_status()"

# Run the MCP server
CMD ["python", "server.py"]
