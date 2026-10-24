# Copyright 2026 KAITEN INC
# SPDX-License-Identifier: Apache-2.0

"""Kaiten, reached through the official SDK.

The MCP server declares no API contract of its own: paths, request bodies,
models, enums and the enforcement arithmetic all come from ``kaitencloud``,
which is generated from Kaiten's OpenAPI document. One client is shared by the
whole process; its credential is a callable, read before every request, that
returns the bearer token of whoever is calling the MCP server -- so an agent
never acts with more than its own token allows.
"""

from __future__ import annotations

from typing import Any

from fastmcp.exceptions import ToolError
from fastmcp.server.dependencies import get_http_request
from fastmcp.server.middleware import Middleware
from kaitencloud import APIStatusError, AsyncKaitenClient, KaitenError, NotFoundError

from .config import settings

_client: AsyncKaitenClient | None = None


def caller_token() -> str:
    """The calling client's token: its Authorization header, else KAITEN_API_TOKEN (stdio)."""
    try:
        header = get_http_request().headers.get("authorization") or ""
    except RuntimeError:  # not inside an HTTP request: stdio
        header = ""
    token = header or settings.api_token
    if token.lower().startswith("bearer "):
        token = token[7:]
    if not token.strip():
        raise ToolError(
            "No Kaiten credential: send 'Authorization: Bearer ksh_...' to the MCP server, "
            "or set KAITEN_API_TOKEN for the stdio transport."
        )
    return token.strip()


def kaiten() -> AsyncKaitenClient:
    global _client
    if _client is None:
        _client = AsyncKaitenClient(
            token=caller_token, base_url=settings.api_base_url, timeout=settings.timeout
        )
    return _client


def use(client: AsyncKaitenClient | None) -> None:
    """Swap the shared client; tests point it at a mocked transport."""
    global _client
    _client = client


async def get_or_none(call: Any) -> Any:
    """Await an SDK read, mapping 404 to None."""
    try:
        return await call
    except NotFoundError:
        return None


async def require(call: Any, what: str) -> Any:
    """Await an SDK read, mapping 404 to an error that tells the agent how to recover."""
    try:
        return await call
    except NotFoundError as e:
        raise ToolError(
            f"No {what} found ({e.detail}). Use kaiten_search to find the right slug."
        ) from e


# The SDK wraps every operation an application calls; the targeting authoring
# endpoints, AI control-plane endpoints and OFREP evaluation need a transport fallback.
# These go through the SDK's own transport (credential, retries, typed errors)
# and decode to plain JSON, until sdk-python grows methods for them.


async def call_unwrapped(
    method: str,
    path: str,
    body: Any = None,
    *,
    idempotent: bool | None = None,
    params: dict[str, Any] | None = None,
) -> Any:
    api = kaiten()._api
    response = await api.request(method, path, body=body, idempotent=idempotent, params=params)
    return api.decode(response)


def describe(e: APIStatusError) -> str:
    message = f"Kaiten API {e.status_code} {e.code or ''}: {e.detail or e.title or ''}".replace(
        "  ", " "
    )
    for err in e.errors or []:
        where = getattr(err, "location", None) or ""
        message += f"\n  - {where}: {getattr(err, 'message', err)}"
    return message


class KaitenErrors(Middleware):
    """Turn SDK errors into tool errors that carry the API's code and detail, the parts an agent can act on."""

    async def on_call_tool(self, context, call_next):
        try:
            return await call_next(context)
        except APIStatusError as e:
            raise ToolError(describe(e)) from e
        except KaitenError as e:
            raise ToolError(f"Kaiten: {e}") from e
