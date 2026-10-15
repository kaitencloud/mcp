"""Process configuration, read once from the environment."""

from __future__ import annotations

import os
from dataclasses import dataclass


def _flag(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    # Where the MCP server sends its API calls. It must be the gateway (Envoy),
    # not the API container: the gateway is what exchanges a ksh_ token for the
    # internal JWT the API trusts, and a client's ksh_ token reaches this
    # server untouched.
    api_base_url: str
    # Used when no request header carries a credential: the stdio transport,
    # or a single-user HTTP deployment.
    api_token: str
    # Register only the tools that read. The credential still bounds what an
    # agent can do; this bounds what it can even attempt.
    read_only: bool
    host: str
    port: int
    timeout: float


def load() -> Settings:
    base = (
        os.getenv("KAITEN_API_BASE_URL")
        or os.getenv("KAITEN_API_URL")
        or "http://localhost:6060/api"
    )
    return Settings(
        api_base_url=base.rstrip("/"),
        api_token=os.getenv("KAITEN_API_TOKEN", "").strip(),
        read_only=_flag("KAITEN_MCP_READ_ONLY"),
        host=os.getenv("MCP_HOST", "0.0.0.0"),
        port=int(os.getenv("MCP_PORT", "8000")),
        timeout=float(os.getenv("KAITEN_API_TIMEOUT", "30")),
    )


settings = load()
