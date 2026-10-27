# Copyright 2026 KAITEN INC
# SPDX-License-Identifier: Apache-2.0

"""Tools driven through a real MCP client and the real SDK, over a mocked Kaiten."""

import json

import httpx
import pytest
from fastmcp import Client
from kaitencloud import AsyncKaitenClient

from kaiten_mcp import sdk
from kaiten_mcp.server import build

CUSTOMER = {
    "id": "c1",
    "slug": "acme",
    "name": "Acme",
    "domain": "acme.com",
    "createdAt": "2026-01-01T00:00:00Z",
    "updatedAt": "2026-01-01T00:00:00Z",
    "createdBy": {"id": "u", "name": "u"},
    "updatedBy": {"id": "u", "name": "u"},
}


def kaiten(handler):
    """Point the shared SDK client at a handler; record every request it gets."""
    seen: list[httpx.Request] = []

    def record(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    sdk.use(
        AsyncKaitenClient(
            token=lambda: "ksh_test",
            base_url="http://kaiten.test",
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(record)),
            max_retries=0,
        )
    )
    return seen


@pytest.fixture(autouse=True)
def fresh_client():
    yield
    sdk.use(None)


async def call(tool, **args):
    async with Client(build(writes=True)) as c:
        return await c.call_tool(tool, args, raise_on_error=False)


async def test_a_save_that_changes_nothing_writes_nothing():
    seen = kaiten(lambda r: httpx.Response(200, json=CUSTOMER))
    result = await call("kaiten_save_customer", customer="acme", name="Acme", domain="acme.com")
    assert result.structured_content["status"] == "unchanged"
    assert [r.method for r in seen] == ["GET"]
    assert seen[0].headers["authorization"] == "Bearer ksh_test"


async def test_a_save_reports_exactly_what_changed():
    state = dict(CUSTOMER)

    def handler(r: httpx.Request) -> httpx.Response:
        if r.method == "PUT":
            state.update(json.loads(r.content))
            return httpx.Response(204)
        return httpx.Response(200, json=state)

    kaiten(handler)
    result = await call("kaiten_save_customer", customer="acme", domain="acme.io")
    assert result.structured_content["status"] == "updated"
    assert result.structured_content["changes"] == {"domain": {"from": "acme.com", "to": "acme.io"}}


async def test_api_errors_reach_the_agent_with_their_code():
    kaiten(
        lambda r: httpx.Response(
            404, json={"status": 404, "code": "Customer.NotFound", "detail": "no such customer"}
        )
    )
    result = await call("kaiten_get_customer", customer="ghost")
    assert result.is_error
    assert "no such customer" in result.content[0].text


async def test_read_only_mode_registers_no_write_tool():
    async with Client(build(writes=False)) as c:
        tools = await c.list_tools()
    assert tools and all(t.annotations.read_only_hint for t in tools)


async def test_every_tool_declares_its_effect():
    async with Client(build(writes=True)) as c:
        tools = await c.list_tools()
    assert len(tools) == 31
    for t in tools:
        assert t.annotations.read_only_hint is not None, t.name
        assert t.description, t.name
