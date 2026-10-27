# Copyright 2026 KAITEN INC
# SPDX-License-Identifier: Apache-2.0

"""Credential forwarding across HTTP callers and the stdio fallback, without a live API."""

import asyncio
import json
import sys
from contextlib import asynccontextmanager
from dataclasses import replace
from pathlib import Path

import httpx
import pytest
from fastmcp import Client
from fastmcp.client.transports import StdioTransport
from fastmcp.exceptions import ToolError
from fastmcp.utilities.tests import ASGIServer, run_asgi_lifespan
from kaitencloud import AsyncKaitenClient
from starlette.requests import Request
from test_tools import CUSTOMER

from kaiten_mcp import sdk
from kaiten_mcp.server import build, http_app

EMPTY_OR_MALFORMED_HEADERS = [
    None,
    "",
    " ",
    "Bearer",
    "Bearer ",
    "Bearer    ",
    "Basic ksh_caller",
    "ksh_caller",
    "Bearer ksh_first ksh_second",
    "Bearer ksh_first\tksh_second",
]


def request(header: str | None = None) -> Request:
    headers = [] if header is None else [(b"authorization", header.encode())]
    return Request({"type": "http", "headers": headers})


@pytest.mark.parametrize(
    "header", ["Bearer ksh_caller", "bEaReR ksh_caller", "Bearer  ksh_caller "]
)
def test_http_credential_takes_precedence(monkeypatch, header):
    monkeypatch.setattr(sdk, "settings", replace(sdk.settings, api_token="ksh_process"))
    monkeypatch.setattr(sdk, "get_http_request", lambda: request(header))
    assert sdk.caller_token() == "ksh_caller"


@pytest.mark.parametrize(
    ("token", "expected"),
    [
        ("ksh_process", "ksh_process"),
        ("Bearer ksh_process", "ksh_process"),
        (" ksh_process ", "ksh_process"),
        ("", None),
        (" ", None),
        ("Bearer ", None),
    ],
)
def test_stdio_credential_or_missing_credential(monkeypatch, token, expected):
    def outside_http():
        raise RuntimeError("No HTTP request")

    monkeypatch.setattr(sdk, "settings", replace(sdk.settings, api_token=token))
    monkeypatch.setattr(sdk, "get_http_request", outside_http)
    if expected is not None:
        assert sdk.caller_token() == expected
    else:
        with pytest.raises(ToolError, match="No Kaiten credential"):
            sdk.caller_token()


@pytest.mark.parametrize("process_token", ["", "ksh_process"])
@pytest.mark.parametrize("header", EMPTY_OR_MALFORMED_HEADERS)
def test_http_without_a_credential_is_rejected(monkeypatch, header, process_token):
    monkeypatch.setattr(sdk, "settings", replace(sdk.settings, api_token=process_token))
    monkeypatch.setattr(sdk, "get_http_request", lambda: request(header))
    with pytest.raises(ToolError, match="No Kaiten credential"):
        sdk.caller_token()


async def test_shared_sdk_reads_each_callers_credential(monkeypatch):
    seen = []

    def record(req):
        seen.append(req.headers["authorization"])
        return httpx.Response(200, json={})

    async with httpx.AsyncClient(transport=httpx.MockTransport(record)) as transport:
        client = AsyncKaitenClient(
            token=sdk.caller_token,
            base_url="http://kaiten.test",
            http_client=transport,
            max_retries=0,
        )
        for token in ("ksh_first", "ksh_second"):
            monkeypatch.setattr(
                sdk, "get_http_request", lambda token=token: request(f"Bearer {token}")
            )
            await client._api.request("GET", "/healthz")
    assert seen == ["Bearer ksh_first", "Bearer ksh_second"]


@asynccontextmanager
async def http_server(monkeypatch, handler):
    """Run the production HTTP app with its real request context and a mocked gateway."""
    monkeypatch.setattr(sdk, "settings", replace(sdk.settings, api_token="ksh_process"))
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as upstream:
        sdk.use(
            AsyncKaitenClient(
                token=sdk.caller_token,
                base_url="http://kaiten.test",
                http_client=upstream,
            )
        )
        try:
            app = http_app(build(writes=True))
            async with run_asgi_lifespan(app):
                yield ASGIServer(url="http://127.0.0.1/mcp", app=app, transport_type="http")
        finally:
            sdk.use(None)


async def save(server, headers):
    # Do not replace get_http_request or caller_token: exercise real HTTP dispatch.
    async with server.client(headers=headers) as client:
        return await client.call_tool(
            "kaiten_save_customer",
            {"customer": "acme", "name": "Acme", "domain": "acme.com"},
            raise_on_error=False,
        )


async def test_real_http_context_isolates_callers_and_never_uses_the_process_token(monkeypatch):
    seen = []

    async def record(req):
        seen.append(req.headers["authorization"])
        await asyncio.sleep(0)  # Keep upstream calls overlapping across request contexts.
        return httpx.Response(200, json=CUSTOMER)

    async with http_server(monkeypatch, record) as server:
        results = await asyncio.gather(
            save(server, {"Authorization": "Bearer ksh_first"}),
            save(server, {"Authorization": "bEaReR  ksh_second "}),
            save(server, {}),
            save(server, {"Authorization": ""}),
        )
        assert all(not result.is_error for result in results[:2])
        assert all(result.is_error for result in results[2:])
        assert (await save(server, {})).is_error  # No bleed from earlier authenticated calls.
    assert sorted(seen) == ["Bearer ksh_first", "Bearer ksh_second"]


@pytest.mark.parametrize("header", EMPTY_OR_MALFORMED_HEADERS)
async def test_real_http_rejects_missing_or_malformed_credentials_before_the_api(
    monkeypatch, header
):
    seen = []

    def record(req):
        seen.append(req.headers["authorization"])
        return httpx.Response(200, json=CUSTOMER)

    async with http_server(monkeypatch, record) as server:
        headers = {} if header is None else {"Authorization": header}
        result = await save(server, headers)
        assert result.is_error and "No Kaiten credential" in result.content[0].text
    assert seen == []


@pytest.mark.parametrize(
    ("status", "code"), [(401, "ValidateToken.Invalid"), (403, "Auth.MissingScope")]
)
async def test_real_http_denial_does_not_retry_with_the_process_token(monkeypatch, status, code):
    seen = []

    def record(req):
        seen.append(req.headers["authorization"])
        return httpx.Response(status, json={"status": status, "code": code, "detail": "denied"})

    async with http_server(monkeypatch, record) as server:
        result = await save(server, {"Authorization": "Bearer ksh_caller"})
        assert result.is_error and code in result.content[0].text
    assert seen == ["Bearer ksh_caller"]


@pytest.mark.parametrize("token", ["ksh_process", ""])
async def test_real_stdio_uses_only_its_configured_credential(token):
    # The subprocess runs the real stdio transport, with no HTTP request context or live API.
    script = f"""
import asyncio
import httpx
from kaitencloud import AsyncKaitenClient
from kaiten_mcp import sdk
from kaiten_mcp.server import build

def record(request):
    assert request.headers['authorization'] == 'Bearer ksh_process'
    return httpx.Response(200, json={json.dumps(CUSTOMER)})

async def main():
    async with httpx.AsyncClient(transport=httpx.MockTransport(record)) as upstream:
        sdk.use(AsyncKaitenClient(token=sdk.caller_token, base_url='http://kaiten.test', http_client=upstream))
        await build(writes=True).run_async(transport='stdio', show_banner=False)

asyncio.run(main())
"""
    transport = StdioTransport(
        command=sys.executable,
        args=["-c", script],
        env={
            "KAITEN_API_TOKEN": token,
            "PYTHONPATH": str(Path(sdk.__file__).resolve().parents[1]),
        },
    )
    async with Client(transport, timeout=10) as client:
        result = await client.call_tool(
            "kaiten_save_customer",
            {"customer": "acme", "name": "Acme", "domain": "acme.com"},
            raise_on_error=False,
        )
    if token:
        assert not result.is_error and result.structured_content["status"] == "unchanged"
    else:
        assert result.is_error and "No Kaiten credential" in result.content[0].text
