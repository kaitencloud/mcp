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


STAMPS = {"createdAt": "2026-01-01T00:00:00Z", "updatedAt": "2026-01-01T00:00:00Z"}
INSTANCE = {
    "id": "i1",
    "slug": "tako-prod",
    "name": "Tako Production",
    "description": "",
    "customerId": "c1",
    "customerSlug": "tako",
    "licenseId": "l1",
    "licenseSlug": "standard",
    "startLicenseDate": "2026-01-01T00:00:00Z",
    "endLicenseDate": "2099-01-01T00:00:00Z",
    "createdBy": {"id": "u", "name": "u"},
    "updatedBy": {"id": "u", "name": "u"},
    **STAMPS,
}
# What an add-on and an exception add to the License's grant, as a usage read's provenance says.
ADDON = {
    "addons": [{"quantity": 1, "overrideBehavior": "ADD", "value": {"type": "number", "value": 5}}]
}
EXCEPTION = {"exception": {"kind": "ADD", "value": {"type": "number", "value": 5}}}


def drivers(used: float, limit: float, layers: dict) -> dict:
    """A usage read of delivery-drivers. Its limit is the effective one, which the API enforces."""
    return {
        "entitlementId": "e1",
        "entitlementSlug": "delivery-drivers",
        "licenseId": "l1",
        "licenseSlug": "standard",
        "value": {"type": "number", "value": used, "event_count": 1},
        "limit": {"type": "number", "value": limit},
        "limitCapExceededOveragePercent": 20,
        "source": "license",
        "provenance": {"addons": [], "boosts": [], "exception": None, **layers},
    }


def a_kaiten_where(usage: dict, *, grant: float, reported: dict | None = None):
    """tako-prod, whose License grants `grant` drivers, whose usage read is `usage`, and whose
    next usage report answers `reported`."""
    routes = {
        ("GET", "/instances"): [INSTANCE],
        ("GET", "/instances/tako-prod"): INSTANCE,
        ("GET", "/instances/tako-prod/entitlements/usage"): [usage],
        ("POST", "/instances/tako-prod/entitlements/delivery-drivers/usage"): reported,
        ("GET", "/licenses/standard/entitlements"): [
            {
                "entitlementSlug": "delivery-drivers",
                "entitlementName": "Drivers",
                "entitlementType": "NUMBER",
                "licenseId": "l1",
                "licenseSlug": "standard",
                "value": {"type": "number", "value": grant},
                "limitCapExceededOveragePercent": 20,
                "createdBy": {"id": "u", "name": "u"},
                "updatedBy": {"id": "u", "name": "u"},
                **STAMPS,
            }
        ],
        ("GET", "/licenses"): [
            {
                "id": "l1",
                "slug": "standard",
                "name": "Standard",
                "description": "",
                "type": "PAID",
                "version": "1",
                "isDefault": False,
                "familyId": "f1",
                **STAMPS,
            }
        ],
        ("GET", "/entitlements"): [
            {
                "id": "e1",
                "slug": "delivery-drivers",
                "name": "Drivers",
                "description": "",
                "type": "NUMBER",
                "warningThresholdPercent": 80,
                **STAMPS,
            }
        ],
        ("GET", "/license-families"): [
            {"id": "f1", "slug": "standard", "versionCount": 1, **STAMPS}
        ],
    }
    # The SDK sends every operation under /api.
    kaiten(lambda r: httpx.Response(200, json=routes[(r.method, r.url.path.removeprefix("/api"))]))


@pytest.mark.parametrize(
    ("layers", "grant", "used", "limit"),
    [(ADDON, 5, 5, 10), (EXCEPTION, 10, 8, 15)],
    ids=["add-on", "exception"],
)
async def test_usage_is_measured_against_the_effective_limit(layers, grant, used, limit):
    # Against the License's grant alone, 5 of 5 would be at_limit and 8 of 10 in warning: both
    # past min_percent=80, and both a reason for attention.
    a_kaiten_where(drivers(used, limit, layers), grant=grant)
    row = (await call("kaiten_get_usage")).structured_content["usage"][0]
    assert (row["used"], row["limit"], row["state"]) == (used, limit, "ok")
    assert row["limit_includes"] == ["add-ons" if layers is ADDON else "exception"]
    assert (await call("kaiten_get_usage", min_percent=80)).structured_content["total"] == 0
    detail = await call("kaiten_get_instance", instance="tako-prod", recent_events=0)
    assert detail.structured_content["limits"][0]["limit"] == limit
    assert detail.structured_content["attention"] == []


async def test_a_report_is_judged_against_the_effective_limit():
    # 12 against a grant of 10 and 20% allowance would be overage, at the hard ceiling.
    a_kaiten_where(drivers(8, 15, EXCEPTION), grant=10, reported=drivers(12, 15, EXCEPTION))
    result = await call(
        "kaiten_report_usage",
        instance="tako-prod",
        entitlement="delivery-drivers",
        amount=12,
        behavior="set",
    )
    usage = result.structured_content["usage"]
    assert (usage["used"], usage["limit"], usage["percent"], usage["state"]) == (
        12,
        15,
        80.0,
        "warning",
    )


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
