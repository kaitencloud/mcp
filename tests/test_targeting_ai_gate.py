# Copyright 2026 KAITEN INC
# SPDX-License-Identifier: Apache-2.0

"""Core's original-type metadata, without catalogue RPCs or JSON/CEL scrubbing."""

from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError
from kaitencloud.types import LicenseEntitlement
from test_ai_gate import grant, license_
from test_tools import kaiten

from kaiten_mcp import ai_policy, sdk
from kaiten_mcp.server import build

TYPES = {"credits": "NUMBER_AI_CREDIT", "ai": "NUMBER", "settings": "CONFIG"}
SCHEMA = {
    "roots": [
        {
            "name": "__kaiten",
            "type": "object",
            "fields": [
                {
                    "name": "entitlements",
                    "type": "map",
                    "knownKeys": ["credits", "ai", "settings", "unknown"],
                    "entries": [
                        {
                            "name": slug,
                            "type": "map" if kind == "CONFIG" else "object",
                            "entitlementType": kind,
                        }
                        for slug, kind in TYPES.items()
                    ]
                    + [{"name": "unknown", "type": "object"}],
                },
                {"name": "instance", "type": "object", "fields": [{"name": "metadata"}]},
            ],
        },
        {"name": "targetingKey", "type": "string"},
    ]
}
FACTS = {
    "entitlements": {
        "credits": {"remaining": "999999999999.999999"},
        "ai": {"used": 1, "limit": 10},
        "settings": {"entitlementType": "NUMBER_AI_CREDIT", "credits": "customer config"},
        "unknown": {"used": 2},
    },
    "instance": {"metadata": {"entitlements": {"credits": "metadata stays"}}},
    "license": {"slug": "ordinary"},
}


@pytest.fixture(autouse=True)
def fresh_client():
    yield
    sdk.use(None)


@pytest.mark.parametrize("enabled", [False, True])
async def test_targeting_schema_uses_original_type_without_extra_scopes(enabled):
    seen = kaiten(lambda request: httpx.Response(200, json=SCHEMA))
    async with Client(build(writes=False, ai_enabled=enabled)) as client:
        result = await client.call_tool("kaiten_test_rule", {})
    body = result.structured_content
    node = body["roots"][0]["fields"][0]
    if enabled:
        assert body == SCHEMA
    else:
        assert node["knownKeys"] == ["ai", "settings"]
        assert [entry["name"] for entry in node["entries"]] == ["ai", "settings"]
        assert body["roots"][0]["fields"][1] == SCHEMA["roots"][0]["fields"][1]
    assert [(r.method, r.url.path) for r in seen] == [
        ("GET", "/api/feature-flags/targeting/context")
    ]


@pytest.mark.parametrize("enabled", [False, True])
async def test_rehearsal_projects_only_reserved_entitlements(enabled):
    seen = kaiten(
        lambda request: httpx.Response(
            200,
            json={
                "valid": True,
                "matched": True,
                "issues": [],
                "facts": FACTS,
                "entitlementTypes": TYPES,
            },
        )
    )
    async with Client(build(writes=False, ai_enabled=enabled)) as client:
        result = await client.call_tool("kaiten_test_rule", {"rule": "true"})
    body = result.structured_content
    assert body["valid"] is True and body["matched"] is True
    facts = body["facts"]
    if enabled:
        assert facts == FACTS
    else:
        assert facts["entitlements"] == {
            "ai": FACTS["entitlements"]["ai"],
            "settings": FACTS["entitlements"]["settings"],
        }
        assert facts["instance"] == FACTS["instance"]
        assert facts["license"] == FACTS["license"]
    assert [(r.method, r.url.path) for r in seen] == [("POST", "/api/feature-flags/targeting/test")]


def test_projection_never_mutates_canonical_payloads():
    schema, facts = deepcopy(SCHEMA), deepcopy(FACTS)
    ai_policy.targeting_context(schema, enabled=False)
    ai_policy.targeting_facts(facts, TYPES, enabled=False)
    assert schema == SCHEMA
    assert facts == FACTS


@pytest.mark.parametrize(
    "types", [None, {}, {"credits": "unknown"}, {"credits": {"type": "NUMBER"}}]
)
def test_unclassified_legacy_facts_are_not_assumed_ordinary(types):
    projected = ai_policy.targeting_facts(FACTS, types, enabled=False)
    assert projected["entitlements"] == {}
    assert projected["instance"] == FACTS["instance"]


async def test_legacy_schema_does_not_disclose_unclassified_entitlement_keys():
    legacy = deepcopy(SCHEMA)
    for node in legacy["roots"][0]["fields"][0]["entries"]:
        node.pop("entitlementType", None)
    seen = kaiten(lambda request: httpx.Response(200, json=legacy))
    async with Client(build(writes=False, ai_enabled=False)) as client:
        result = await client.call_tool("kaiten_test_rule", {})
    assert result.structured_content["roots"][0]["fields"][0]["knownKeys"] == []
    assert len(seen) == 1


@pytest.mark.parametrize("facts", [[{"credits": "untyped"}], {"entitlements": ["untyped"]}])
def test_malformed_reserved_facts_do_not_return_unclassified_payloads(facts):
    with pytest.raises(ToolError, match="structured"):
        ai_policy.targeting_facts(facts, TYPES, enabled=False)


@pytest.mark.parametrize("payload", [{"roots": "untyped"}, {"roots": ["untyped"]}])
def test_malformed_context_fails_closed(payload):
    with pytest.raises(ToolError, match="structured"):
        ai_policy.targeting_context(payload, enabled=False)


async def test_license_view_does_not_acquire_a_catalogue_read_permission(monkeypatch):
    from kaiten_mcp.tools.catalog import _version_view

    catalogue = AsyncMock(side_effect=RuntimeError("catalogue read is not permitted"))
    k = SimpleNamespace(
        entitlements=SimpleNamespace(list=catalogue),
        licenses=SimpleNamespace(
            list_entitlements=AsyncMock(
                return_value=[grant("quota", "NUMBER"), grant("credits", "NUMBER_AI_CREDIT")]
            )
        ),
    )
    monkeypatch.setattr(sdk, "kaiten", lambda: k)
    result = await _version_view(
        license_(), [], list_instances=False, plan="premium", ai_enabled=False
    )
    assert [row["entitlement"] for row in result["grants"]] == ["quota"]
    catalogue.assert_not_awaited()


def test_installed_sdk_retains_the_original_grant_discriminator():
    value = LicenseEntitlement.model_validate(
        {
            "entitlementSlug": "credits",
            "entitlementType": "NUMBER_AI_CREDIT",
            "value": {"type": "number", "value": 10},
            "entitlementName": "Credits",
            "licenseId": "l1",
            "licenseSlug": "premium-v1",
            "createdAt": "2026-01-01T00:00:00Z",
            "updatedAt": "2026-01-01T00:00:00Z",
            "createdBy": {"id": "u1", "name": "Synthetic user"},
            "updatedBy": {"id": "u1", "name": "Synthetic user"},
        }
    )
    assert value.entitlement_type == "NUMBER_AI_CREDIT"
