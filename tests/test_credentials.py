# Copyright 2026 KAITEN INC
# SPDX-License-Identifier: Apache-2.0

"""Credential forwarding across HTTP callers and the stdio fallback, without a live API."""

from dataclasses import replace

import httpx
import pytest
from fastmcp.exceptions import ToolError
from kaitencloud import AsyncKaitenClient
from starlette.requests import Request

from kaiten_mcp import sdk


def request(header: str = "") -> Request:
    return Request({"type": "http", "headers": [(b"authorization", header.encode())]})


@pytest.mark.parametrize(
    "header", ["Bearer ksh_caller", "bEaReR ksh_caller", "Bearer  ksh_caller "]
)
def test_http_credential_takes_precedence(monkeypatch, header):
    monkeypatch.setattr(sdk, "settings", replace(sdk.settings, api_token="ksh_process"))
    monkeypatch.setattr(sdk, "get_http_request", lambda: request(header))
    assert sdk.caller_token() == "ksh_caller"


def test_http_without_a_header_uses_the_process_credential(monkeypatch):
    monkeypatch.setattr(sdk, "settings", replace(sdk.settings, api_token="ksh_process"))
    monkeypatch.setattr(sdk, "get_http_request", request)
    assert sdk.caller_token() == "ksh_process"


@pytest.mark.parametrize("token", ["ksh_process", ""])
def test_stdio_credential_or_missing_credential(monkeypatch, token):
    def outside_http():
        raise RuntimeError("No HTTP request")

    monkeypatch.setattr(sdk, "settings", replace(sdk.settings, api_token=token))
    monkeypatch.setattr(sdk, "get_http_request", outside_http)
    if token:
        assert sdk.caller_token() == token
    else:
        with pytest.raises(ToolError, match="No Kaiten credential"):
            sdk.caller_token()


@pytest.mark.parametrize("header", ["", "Bearer ", "Bearer    "])
def test_http_without_a_credential_is_rejected(monkeypatch, header):
    monkeypatch.setattr(sdk, "settings", replace(sdk.settings, api_token=""))
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


@pytest.mark.parametrize("ai_enabled", [False, True])
async def test_real_http_context_isolates_concurrent_credentials_and_preserves_fallback(
    monkeypatch, ai_enabled
):
    import asyncio

    from fastmcp.utilities.tests import asgi_server
    from test_tools import CUSTOMER

    from kaiten_mcp.server import build

    monkeypatch.setattr(sdk, "settings", replace(sdk.settings, api_token="ksh_process"))
    seen = []

    def record(req):
        seen.append(req.headers["authorization"])
        return httpx.Response(200, json=CUSTOMER)

    async with httpx.AsyncClient(transport=httpx.MockTransport(record)) as upstream:
        sdk.use(
            AsyncKaitenClient(
                token=sdk.caller_token,
                base_url="http://kaiten.test",
                http_client=upstream,
                max_retries=0,
            )
        )
        try:
            async with asgi_server(
                build(writes=True, ai_enabled=ai_enabled), stateless_http=True
            ) as server:

                async def save(headers):
                    # Exercise the real HTTP middleware/request context; never replace
                    # get_http_request or the SDK's caller_token callback here.
                    async with server.client(headers=headers) as client:
                        return await client.call_tool(
                            "kaiten_save_customer",
                            {"customer": "acme", "name": "Acme", "domain": "acme.com"},
                            raise_on_error=False,
                        )

                results = await asyncio.gather(
                    save({"Authorization": "Bearer ksh_first"}),
                    save({"Authorization": "Bearer ksh_second"}),
                )
                assert all(not r.is_error for r in results)
                assert not (await save({})).is_error
                assert (await save({"Authorization": "Bearer "})).is_error
        finally:
            sdk.use(None)
    assert sorted(seen) == ["Bearer ksh_first", "Bearer ksh_process", "Bearer ksh_second"]


async def test_real_http_denial_does_not_retry_with_the_process_token(monkeypatch):
    from fastmcp.utilities.tests import asgi_server

    from kaiten_mcp.server import build

    monkeypatch.setattr(sdk, "settings", replace(sdk.settings, api_token="ksh_process"))
    seen = []

    def record(req):
        seen.append(req.headers["authorization"])
        return httpx.Response(
            403, json={"status": 403, "code": "AIUsage.Disabled", "detail": "disabled"}
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(record)) as upstream:
        sdk.use(
            AsyncKaitenClient(
                token=sdk.caller_token,
                base_url="http://kaiten.test",
                http_client=upstream,
                max_retries=0,
            )
        )
        try:
            async with (
                asgi_server(build(ai_enabled=True), stateless_http=True) as server,
                server.client(headers={"Authorization": "Bearer ksh_caller"}) as client,
            ):
                result = await client.call_tool("kaiten_get_ai_models", {}, raise_on_error=False)
                assert result.is_error and "AIUsage.Disabled" in result.content[0].text
        finally:
            sdk.use(None)
    assert seen == ["Bearer ksh_caller"]
