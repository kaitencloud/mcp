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
