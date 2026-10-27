# Copyright 2026 KAITEN INC
# SPDX-License-Identifier: Apache-2.0

"""Local rollout policy via MCP, with no services or provider calls."""

import json
import os
import subprocess
import sys
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import httpx
import pytest
from fastmcp import Client
from test_tools import kaiten

from kaiten_mcp import config, sdk, server, views
from kaiten_mcp.tools.catalog import _prepare_grants

AI_READS = {"kaiten_get_ai_models", "kaiten_get_ai_rules", "kaiten_get_ai_history"}
AI_WRITES = {"kaiten_save_ai_model", "kaiten_save_ai_rule", "kaiten_report_ai_usage"}


def definition(slug, kind):
    return NS(
        slug=slug,
        type=kind,
        name=slug,
        description="",
        aggregation_method="SUM",
        reset_period=None,
        reset_anchor=None,
        unit_plural=None,
        unit_singular=None,
        warning_threshold_percent=None,
        user_facing=False,
        entitlement_groups=[],
    )


def grant(slug, kind, value=10):
    return NS(
        entitlement_slug=slug,
        entitlement_type=kind,
        value=value,
        limit_cap_exceeded_overage_percent=0,
    )


def license_():
    return NS(
        slug="premium-v1",
        name="Premium",
        description="",
        type="PAID",
        version=1,
        version_name="v1",
        is_default=False,
        family_id="f1",
        family_slug="premium",
        lifecycle_state="PUBLISHED",
    )


@pytest.fixture(autouse=True)
def fresh_client():
    yield
    sdk.use(None)


async def call(tool, **args):
    async with Client(server.build(writes=True, ai_enabled=False)) as client:
        return await client.call_tool(tool, args, raise_on_error=False)


@pytest.mark.parametrize("raw", [None, "", "false", "0", "yes", "1", "on", "garbage", "truee"])
async def test_default_and_malformed_opt_in_hide_schemas_and_calls(monkeypatch, raw):
    if raw is None:
        monkeypatch.delenv("KAITEN_MCP_ENABLE_AI", raising=False)
    else:
        monkeypatch.setenv("KAITEN_MCP_ENABLE_AI", raw)
    settings = config.load()
    assert not settings.enable_ai
    monkeypatch.setattr(server, "settings", settings)
    seen = kaiten(lambda r: httpx.Response(500))
    mcp = server.build(writes=True)
    async with Client(mcp) as client:
        tools = await client.list_tools()
        assert not ({t.name for t in tools} & (AI_READS | AI_WRITES))
        discovery = json.dumps([t.model_dump(by_alias=True) for t in tools]) + mcp.instructions
        assert "NUMBER_AI_CREDIT" not in discovery
        assert "kaiten_get_ai" not in discovery
        assert "kaiten_report_ai" not in discovery
        assert "AI credit" not in discovery
        for name in AI_READS | AI_WRITES:
            assert (await client.call_tool(name, {}, raise_on_error=False)).is_error
    assert not seen


@pytest.mark.parametrize("raw", ["true", " TRUE "])
@pytest.mark.parametrize("writes", [False, True])
async def test_enabled_registration_counts(monkeypatch, raw, writes):
    monkeypatch.setenv("KAITEN_MCP_ENABLE_AI", raw)
    monkeypatch.setattr(server, "settings", config.load())
    async with Client(server.build(writes=writes)) as client:
        names = {t.name for t in await client.list_tools()}
    assert names & (AI_READS | AI_WRITES) == (AI_READS | AI_WRITES if writes else AI_READS)
    assert len(names) == (37 if writes else 18)


def test_fresh_disabled_build_does_not_import_ai_module():
    env = {**os.environ, "KAITEN_MCP_ENABLE_AI": "false", "PYTHONDONTWRITEBYTECODE": "1"}
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; from kaiten_mcp.server import build; build(); "
            "assert 'kaiten_mcp.tools.ai' not in sys.modules; "
            "assert 'kaitencloud.experimental.ai' not in sys.modules",
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == 0, result.stderr


async def test_explicit_ai_creation_fails_without_any_request():
    seen = kaiten(lambda r: httpx.Response(500))
    result = await call(
        "kaiten_save_entitlement", entitlement="credits", name="Credits", type="NUMBER_AI_CREDIT"
    )
    assert result.is_error and not seen


@pytest.mark.parametrize(
    "tool,args",
    [
        ("kaiten_save_entitlement", {"entitlement": "credits", "name": "Changed"}),
        ("kaiten_save_entitlement", {"entitlement": "credits", "groups": ["new-group"]}),
        ("kaiten_delete", {"kind": "entitlement", "slug": "credits", "confirm": True}),
    ],
)
async def test_existing_ai_target_never_writes(tool, args):
    seen = kaiten(
        lambda r: httpx.Response(200, json={"slug": "credits", "type": "NUMBER_AI_CREDIT"})
    )
    result = await call(tool, **args)
    assert result.is_error
    assert [r.method for r in seen] == ["GET"]


@pytest.mark.parametrize("intent", ["new", "edit", "version"])
@pytest.mark.parametrize("ai_value", [None, 20, {"value": 30, "overage_percent": 5}])
async def test_whole_grant_patch_preflight_precedes_license_and_grant_writes(
    monkeypatch, intent, ai_value
):
    lic = license_()
    ents = [definition("quota", "NUMBER"), definition("credits", "NUMBER_AI_CREDIT")]
    existing = [grant("quota", "NUMBER"), grant("credits", "NUMBER_AI_CREDIT")]
    k = NS(
        entitlements=NS(list=AsyncMock(return_value=ents)),
        licenses=NS(
            get=AsyncMock(return_value=lic),
            list_entitlements=AsyncMock(return_value=existing),
            create=AsyncMock(),
            update=AsyncMock(),
            delete_entitlement=AsyncMock(),
            associate_entitlement=AsyncMock(),
            update_entitlement=AsyncMock(),
        ),
        license_families=NS(
            get=AsyncMock(return_value=NS(slug="premium", current_version=lic, versions=[lic]))
        ),
    )
    monkeypatch.setattr(sdk, "kaiten", lambda: k)
    args = {"grants": {"quota": 20, "credits": ai_value}, "name": "Changed"}
    if intent == "new":
        args["type"] = "PAID"
    elif intent == "edit":
        args["license"] = lic.slug
    else:
        args.update(new_version_of="premium", copy_grants=False)
    result = await call("kaiten_save_license", **args)
    assert result.is_error
    for method in (
        "create",
        "update",
        "delete_entitlement",
        "associate_entitlement",
        "update_entitlement",
    ):
        getattr(k.licenses, method).assert_not_awaited()


@pytest.mark.parametrize("remove_hidden", [False, True])
async def test_copy_uses_canonical_grants_and_denies_before_create(monkeypatch, remove_hidden):
    lic = license_()
    k = NS(
        entitlements=NS(list=AsyncMock(return_value=[definition("credits", "NUMBER_AI_CREDIT")])),
        licenses=NS(
            list_entitlements=AsyncMock(return_value=[grant("credits", "NUMBER")]),
            create=AsyncMock(),
        ),
        license_families=NS(
            get=AsyncMock(return_value=NS(slug="premium", current_version=lic, versions=[lic]))
        ),
    )
    monkeypatch.setattr(sdk, "kaiten", lambda: k)
    result = await call(
        "kaiten_save_license",
        new_version_of="premium",
        grants={"credits": None} if remove_hidden else {},
    )
    assert result.is_error
    k.licenses.create.assert_not_awaited()


async def test_ordinary_license_patch_keeps_unmentioned_hidden_grants(monkeypatch):
    lic = license_()
    ents = [definition("quota", "NUMBER"), definition("credits", "NUMBER_AI_CREDIT")]
    existing = [grant("quota", "NUMBER"), grant("credits", "NUMBER_AI_CREDIT")]
    k = NS(
        entitlements=NS(list=AsyncMock(return_value=ents)),
        licenses=NS(
            get=AsyncMock(return_value=lic),
            list_entitlements=AsyncMock(return_value=existing),
            get_entitlement=AsyncMock(return_value=grant("quota", "NUMBER", 20)),
            update=AsyncMock(),
            update_entitlement=AsyncMock(),
            delete_entitlement=AsyncMock(),
        ),
        license_families=NS(list=AsyncMock(return_value=[NS(id="f1", slug="premium")])),
        instances=NS(list=AsyncMock(return_value=[])),
    )
    monkeypatch.setattr(sdk, "kaiten", lambda: k)
    result = await call("kaiten_save_license", license=lic.slug, grants={"quota": 20})
    assert not result.is_error, result
    k.licenses.update_entitlement.assert_awaited_once_with(lic.slug, "quota", 20, overage_percent=0)
    k.licenses.delete_entitlement.assert_not_awaited()
    assert existing[1].entitlement_slug == "credits"


async def test_invalid_later_ordinary_grant_is_preflighted(monkeypatch):
    ents = [definition("quota", "NUMBER"), definition("feature", "BOOLEAN")]
    monkeypatch.setattr(
        sdk, "kaiten", lambda: NS(entitlements=NS(list=AsyncMock(return_value=ents)))
    )
    with pytest.raises(Exception, match="true or false"):
        await _prepare_grants({"quota": 20, "feature": "invalid"}, {}, ai_enabled=False)
    prepared = await _prepare_grants(
        {"quota": {"value": -1, "overage_percent": -1}, "feature": True}, {}, ai_enabled=False
    )
    assert prepared == {"quota": (-1, -1), "feature": (True, None)}


@pytest.mark.parametrize("amount", [1, -1])
@pytest.mark.parametrize("behavior", ["append", "set"])
async def test_numeric_ai_adjustments_deny_before_report(monkeypatch, amount, behavior):
    inst = NS(slug="acme-prod", license_slug="premium-v1")
    snap = NS(
        grants={inst.license_slug: [grant("credits", "NUMBER")]},
        entitlements={"credits": definition("credits", "NUMBER_AI_CREDIT")},
        usage={},
    )
    monkeypatch.setattr(views, "instance", AsyncMock(return_value=inst))
    monkeypatch.setattr(views, "snapshot", AsyncMock(return_value=snap))
    seen = kaiten(lambda r: httpx.Response(500))
    result = await call(
        "kaiten_report_usage",
        instance=inst.slug,
        entitlement="credits",
        amount=amount,
        behavior=behavior,
    )
    assert result.is_error and not seen


@pytest.mark.parametrize("enabled", [False, True])
def test_operational_projection_uses_original_catalog_type_not_metadata(enabled):
    inst = NS(slug="acme-prod", license_slug="premium-v1")
    definitions = [
        definition("credits", "NUMBER_AI_CREDIT"),
        definition("quota", "NUMBER"),
        definition("config", "CONFIG"),
        definition("feature", "BOOLEAN"),
    ]
    config_value = {"ai": {"type": "NUMBER_AI_CREDIT", "units": "0.000001"}}
    grants = [
        grant("credits", "NUMBER"),
        grant("quota", "NUMBER"),
        grant("config", "CONFIG", config_value),
        grant("feature", "BOOLEAN", True),
    ]
    snap = views.Snapshot(
        [inst],
        {},
        {inst.license_slug: grants},
        {e.slug: e for e in definitions},
        {},
        ai_enabled=enabled,
    )
    projected = views.entitlements_view(snap, inst)
    assert {r["entitlement"] for r in projected["limits"]} == (
        {"quota", "credits"} if enabled else {"quota"}
    )
    assert projected["config"] == {"config": config_value}
    assert projected["features"] == {"feature": True}
    assert len(snap.grants[inst.license_slug]) == 4


@pytest.mark.parametrize("enabled", [False, True])
async def test_immutable_audit_financial_payload_is_not_filtered(monkeypatch, enabled):
    payload = {
        "journal": {
            "entitlementType": "NUMBER_AI_CREDIT",
            "charged": "999999999999.999999",
            "vendorCost": "0.000000000001",
        },
        "invoice": {"amount": "1.000001"},
        "metadata": {"ai": True},
    }
    events = [
        NS(
            event_name=name,
            timestamp="2026-01-01T00:00:00Z",
            instance_slug="acme-prod",
            payload=payload,
        )
        for name in ("INSTANCE_AI_USAGE_COMMITTED", "INSTANCE_USAGE_REPORTED")
    ]
    k = NS(
        instances=NS(
            list=AsyncMock(return_value=[NS(slug="acme-prod")]),
            list_audit_trails=AsyncMock(return_value=events),
        )
    )
    monkeypatch.setattr(sdk, "kaiten", lambda: k)
    async with Client(server.build(writes=False, ai_enabled=enabled)) as client:
        result = await client.call_tool("kaiten_get_audit_trail", {"include_payload": True})
    assert [e["event"] for e in result.structured_content["events"]] == [
        e.event_name for e in events
    ]
    assert all(e["payload"] == payload for e in result.structured_content["events"])


async def test_server_disabled_denial_is_authoritative_when_locally_enabled():
    seen = kaiten(
        lambda r: httpx.Response(
            403, json={"status": 403, "code": "AIUsage.Disabled", "detail": "AI is disabled"}
        )
    )
    async with Client(server.build(writes=True, ai_enabled=True)) as client:
        result = await client.call_tool("kaiten_get_ai_models", {}, raise_on_error=False)
    assert result.is_error
    assert "403" in result.content[0].text and "AIUsage.Disabled" in result.content[0].text
    assert [r.method for r in seen] == ["GET"]


@pytest.mark.parametrize("kind", ["license", "instance", "customer", "entitlement_group"])
async def test_cascade_delete_checks_unfiltered_ai_relations_before_write(monkeypatch, kind):
    credits = definition("credits", "NUMBER_AI_CREDIT")
    credits.entitlement_groups = [NS(slug="group")]
    resources = {
        "licenses": NS(
            get=AsyncMock(return_value=license_()),
            delete=AsyncMock(),
            list_entitlements=AsyncMock(return_value=[grant("credits", "NUMBER")]),
        ),
        "instances": NS(
            get=AsyncMock(return_value=NS(slug="target", license_slug="premium-v1")),
            delete=AsyncMock(),
            list=AsyncMock(return_value=[NS(customer_slug="target", license_slug="premium-v1")]),
        ),
        "customers": NS(get=AsyncMock(return_value=NS(slug="target")), delete=AsyncMock()),
        "entitlement_groups": NS(get=AsyncMock(return_value=NS(slug="group")), delete=AsyncMock()),
        "entitlements": NS(list=AsyncMock(return_value=[credits])),
    }
    k = NS(**resources)
    monkeypatch.setattr(sdk, "kaiten", lambda: k)
    result = await call(
        "kaiten_delete",
        kind=kind,
        slug="group" if kind == "entitlement_group" else "target",
        confirm=True,
    )
    assert result.is_error
    for name in ("licenses", "instances", "customers", "entitlement_groups"):
        resources[name].delete.assert_not_awaited()


async def test_ai_provisioning_preflight_does_not_create_customer_first(monkeypatch):
    lic = license_()
    k = NS(
        entitlements=NS(list=AsyncMock(return_value=[definition("credits", "NUMBER_AI_CREDIT")])),
        licenses=NS(list_entitlements=AsyncMock(return_value=[grant("credits", "NUMBER")])),
        customers=NS(create=AsyncMock()),
        instances=NS(create=AsyncMock()),
    )
    monkeypatch.setattr(sdk, "kaiten", lambda: k)
    monkeypatch.setattr(views, "resolve_license", AsyncMock(return_value=lic))
    result = await call(
        "kaiten_provision_instance",
        customer="acme",
        customer_name="Acme",
        name="Acme production",
        plan="premium",
    )
    assert result.is_error
    k.customers.create.assert_not_awaited()
    k.instances.create.assert_not_awaited()


async def test_search_omits_ai_rows_before_totals_and_limit(monkeypatch):
    k = NS(
        entitlements=NS(
            list=AsyncMock(
                return_value=[
                    definition("aa-credits", "NUMBER_AI_CREDIT"),
                    definition("bb-quota", "NUMBER"),
                ]
            )
        )
    )
    monkeypatch.setattr(sdk, "kaiten", lambda: k)
    result = await call("kaiten_search", query="a", kinds=["entitlement"], limit=1)
    assert result.structured_content == {
        "total": 1,
        "matches": [{"kind": "entitlement", "slug": "bb-quota", "name": "bb-quota"}],
    }


async def test_mixed_catalog_keeps_ordinary_grants_and_hides_ai(monkeypatch):
    lic = license_()
    fam = NS(id="f1", slug="premium", current_version=lic, versions=[lic], version_count=1)
    ents = [definition("credits", "NUMBER_AI_CREDIT"), definition("quota", "NUMBER")]
    k = NS(
        entitlements=NS(list=AsyncMock(return_value=ents)),
        licenses=NS(
            list_entitlements=AsyncMock(
                # The wire getter carries the original definition type. Normalized
                # in-memory snapshots have their separate catalogue fallback test.
                return_value=[grant("credits", "NUMBER_AI_CREDIT"), grant("quota", "NUMBER")]
            )
        ),
        license_families=NS(list=AsyncMock(return_value=[fam]), get=AsyncMock(return_value=fam)),
        entitlement_groups=NS(list=AsyncMock(return_value=[])),
        instances=NS(list=AsyncMock(return_value=[])),
    )
    monkeypatch.setattr(sdk, "kaiten", lambda: k)
    result = await call("kaiten_get_catalog")
    assert [e["slug"] for e in result.structured_content["entitlements"]] == ["quota"]
    assert [
        g["entitlement"] for g in result.structured_content["plans"][0]["current"]["grants"]
    ] == ["quota"]
    result = await call("kaiten_get_catalog", plan="premium")
    assert [g["entitlement"] for g in result.structured_content["versions"][0]["grants"]] == [
        "quota"
    ]


async def test_operational_usage_total_and_limit_use_only_visible_grants(monkeypatch):
    inst = NS(slug="acme-prod", license_slug="premium-v1", customer_slug="acme")
    definitions = [definition("credits", "NUMBER_AI_CREDIT"), definition("quota", "NUMBER")]
    snap = views.Snapshot(
        [inst],
        {},
        {inst.license_slug: [grant("credits", "NUMBER"), grant("quota", "NUMBER")]},
        {e.slug: e for e in definitions},
        {},
    )
    monkeypatch.setattr(views, "snapshot", AsyncMock(return_value=snap))
    monkeypatch.setattr(
        sdk, "kaiten", lambda: NS(instances=NS(list=AsyncMock(return_value=[inst])))
    )
    result = await call("kaiten_get_usage", limit=1)
    assert result.structured_content["total"] == 1
    assert result.structured_content["usage"][0]["entitlement"] == "quota"
    assert (await call("kaiten_get_usage", entitlement="credits")).is_error


@pytest.mark.parametrize("new_version", [False, True])
async def test_creation_keeps_unknown_null_grant_as_ordinary_noop(monkeypatch, new_version):
    lic = license_()
    fam = NS(slug="premium", current_version=lic, versions=[lic])
    k = NS(
        entitlements=NS(list=AsyncMock(return_value=[])),
        licenses=NS(
            create=AsyncMock(return_value=lic), list_entitlements=AsyncMock(return_value=[])
        ),
        license_families=NS(get=AsyncMock(return_value=fam)),
    )
    monkeypatch.setattr(sdk, "kaiten", lambda: k)
    args = {"grants": {"unused": None}}
    if new_version:
        args.update(new_version_of="premium", copy_grants=False)
    else:
        args.update(name="Premium", type="PAID")
    result = await call("kaiten_save_license", **args)
    assert not result.is_error, result
    k.licenses.create.assert_awaited_once()
