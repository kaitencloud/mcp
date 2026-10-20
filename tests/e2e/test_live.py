# Copyright 2026 KAITEN INC
# SPDX-License-Identifier: Apache-2.0

"""End to end, against a live Kaiten: every tool, through a real MCP client.

Needs KAITEN_API_BASE_URL (the gateway, e.g. http://localhost:6060/api) and a
KAITEN_API_TOKEN with read:* and write:* on an organization holding the demo
dataset (the Kaiten Sushi Shop that Kaiten's `task quickstart` seeds). Every
resource it creates carries a run-unique suffix and is deleted at the end.

    uv run pytest -m e2e
"""

from __future__ import annotations

import contextlib
import os
import uuid

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.skipif(
        not os.getenv("KAITEN_API_TOKEN"), reason="needs a live Kaiten (KAITEN_API_TOKEN)"
    ),
]

RUN = uuid.uuid4().hex[:6]


@pytest.fixture
async def mcp():
    from kaiten_mcp import sdk
    from kaiten_mcp.server import build

    sdk.use(None)  # a fresh SDK client on this test's event loop
    async with Client(build(writes=True)) as client:

        async def call(tool: str, **args):
            result = await client.call_tool(tool, args)
            return result.structured_content

        yield call


async def test_reads_answer_on_the_demo_dataset(mcp):
    overview = await mcp("kaiten_overview")
    assert overview["counts"]["instances"] > 0 and overview["plans"]
    plan = overview["plans"][0]["plan"]

    found = await mcp("kaiten_search", query=plan)
    assert any(m["kind"] == "plan" and m["slug"] == plan for m in found["matches"])

    catalog = await mcp("kaiten_get_catalog")
    assert catalog["entitlements"] and catalog["plans"]
    versions = await mcp("kaiten_get_catalog", plan=plan)
    assert versions["versions"]

    instances = await mcp("kaiten_list_instances")
    inst = instances["instances"][0]
    detail = await mcp("kaiten_get_instance", instance=inst["slug"])
    assert detail["customer"] == inst["customer"]
    customer = await mcp("kaiten_get_customer", customer=inst["customer"])
    assert inst["slug"] in [i["slug"] for i in customer["instances"]]

    usage = await mcp("kaiten_get_usage", limit=5)
    assert all("state" in row for row in usage["usage"])
    assert (await mcp("kaiten_get_deployments"))["deployment_zones"]
    assert (await mcp("kaiten_get_flags"))["flags"]
    evaluated = await mcp("kaiten_evaluate_flags", instance=inst["slug"])
    assert evaluated["context"]["targetingKey"] == inst["customer"]
    assert "roots" in await mcp("kaiten_test_rule")
    assert "service_accounts" in await mcp("kaiten_get_access")
    assert "fields" in await mcp("kaiten_get_metadata_fields")
    assert "connectors" in await mcp("kaiten_get_connectors")
    assert "events" in await mcp("kaiten_get_audit_trail", limit=3)


async def test_commercial_lifecycle_end_to_end(mcp):
    """Define an entitlement, a plan and a customer; provision, meter, upgrade, renew; tear it all down."""
    ent, plan, cust = f"e2e-seats-{RUN}", f"e2e-plan-{RUN}", f"e2e-cust-{RUN}"
    created_instances: list[str] = []
    try:
        r = await mcp(
            "kaiten_save_entitlement",
            entitlement=ent,
            name="E2E seats",
            type="NUMBER",
            aggregation="LATEST",
            unit_singular="seat",
            unit_plural="seats",
        )
        assert r["status"] == "created"
        assert (await mcp("kaiten_save_entitlement", entitlement=ent, name="E2E seats"))[
            "status"
        ] == "unchanged"
        r = await mcp(
            "kaiten_save_entitlement",
            entitlement=ent,
            warning_threshold_percent=80,
            groups=[f"e2e-group-{RUN}"],
        )
        assert r["status"] == "updated" and r["entitlement"]["groups"] == [f"e2e-group-{RUN}"]

        r = await mcp(
            "kaiten_save_license", name="E2E Plan", type="PAID", slug=plan, grants={ent: 5}
        )
        assert r["status"] == "created" and r["license"]["grants"][0]["value"] == 5
        assert r["license"]["grants"][0]["limit"] == "hard"

        r = await mcp("kaiten_save_customer", customer=cust, name="E2E Customer")
        assert r["status"] == "created"
        assert (await mcp("kaiten_save_customer", customer=cust, name="E2E Customer"))[
            "status"
        ] == "unchanged"
        r = await mcp("kaiten_save_customer", customer=cust, domain="e2e.example")
        assert r["status"] == "updated" and r["changes"] == {
            "domain": {"from": None, "to": "e2e.example"}
        }

        r = await mcp(
            "kaiten_provision_instance",
            customer=cust,
            name="E2E Prod",
            slug=f"e2e-inst-{RUN}",
            plan=plan,
        )
        assert r["status"] == "created"
        inst = r["instance"]["slug"]
        created_instances.append(inst)
        assert r["instance"]["license"] == plan and r["instance"]["limits"][0]["limit"] == 5
        assert r["instance"]["plan"] == plan
        assert [
            i["slug"] for i in (await mcp("kaiten_list_instances", plan=plan))["instances"]
        ] == [inst]

        r = await mcp(
            "kaiten_report_usage", instance=inst, entitlement=ent, amount=4, behavior="set"
        )
        assert (
            r["status"] == "recorded"
            and r["usage"]["used"] == 4
            and r["usage"]["state"] == "warning"
        )
        r = await mcp(
            "kaiten_report_usage", instance=inst, entitlement=ent, amount=9, behavior="set"
        )
        assert r["status"] == "rejected" and r["usage"]["used"] == 4

        # A new version with more seats and a soft limit; nobody is on it yet.
        r = await mcp(
            "kaiten_save_license",
            new_version_of=plan,
            grants={ent: {"value": 10, "overage_percent": 20}},
        )
        assert (
            r["status"] == "created"
            and r["license"]["version"] == "2"
            and r["license"]["instances"] == []
        )
        v2 = r["license"]["slug"]

        r = await mcp(
            "kaiten_update_instance",
            instance=inst,
            plan=plan,
            extend_license_days=30,
            metadata={"environment": "staging"},
            status="DEGRADED",
        )
        assert r["status"] == "updated"
        assert set(r["changes"]) >= {"license", "license_end", "metadata", "status"}
        assert r["changes"]["license"] == {"from": plan, "to": v2}
        assert (await mcp("kaiten_update_instance", instance=inst, plan=plan, status="DEGRADED"))[
            "status"
        ] == "unchanged"

        r = await mcp(
            "kaiten_report_usage", instance=inst, entitlement=ent, amount=11, behavior="set"
        )
        assert (
            r["status"] == "recorded"
            and r["usage"]["state"] == "overage"
            and r["usage"]["hard_ceiling"] == 12
        )
        rows = (await mcp("kaiten_get_usage", plan=plan, states=["overage"]))["usage"]
        assert [(u["instance"], u["entitlement"]) for u in rows] == [(inst, ent)]

        # Editing the version in place reports the instances it reaches.
        r = await mcp(
            "kaiten_save_license", license=v2, grants={ent: {"value": 12, "overage_percent": 20}}
        )
        assert r["status"] == "updated" and r["affected_instances"] == [inst]
        assert (
            await mcp(
                "kaiten_save_license",
                license=v2,
                grants={ent: {"value": 12, "overage_percent": 20}},
            )
        )["status"] == "unchanged"

        assert (await mcp("kaiten_set_license_state", license=plan, action="archive"))[
            "status"
        ] == "updated"
        assert (await mcp("kaiten_set_license_state", license=plan, action="archive"))[
            "status"
        ] == "unchanged"
        assert (await mcp("kaiten_set_license_state", license=v2, action="make_default"))[
            "license"
        ]["is_default"] is True
        assert (await mcp("kaiten_set_license_state", license=v2, action="unset_default"))[
            "status"
        ] == "updated"

        catalog = await mcp("kaiten_get_catalog", plan=plan)
        assert [v["state"] for v in catalog["versions"]] == ["ARCHIVED", "PUBLISHED"]

        preview = await mcp("kaiten_delete", kind="license", slug=v2)
        assert preview["status"] == "preview" and preview["impact"]["instances"] == [inst]
        refused = await mcp("kaiten_delete", kind="license", slug=v2, confirm=True)
        assert refused["status"] == "rejected"
    finally:
        for inst in created_instances:
            await mcp("kaiten_delete", kind="instance", slug=inst, confirm=True)
        await _cleanup(mcp, cust=cust, plan=plan, ent=ent)


async def _cleanup(mcp, *, cust: str, plan: str, ent: str) -> None:
    catalog = await mcp("kaiten_get_catalog")
    for p in [p for p in catalog["plans"] if p["plan"] == plan] or [{"plan": plan}]:
        try:
            versions = (await mcp("kaiten_get_catalog", plan=p["plan"]))["versions"]
        except ToolError:
            versions = []
        for v in versions:
            await mcp(
                "kaiten_save_license",
                license=v["slug"],
                grants={g["entitlement"]: None for g in v["grants"]},
            )
            await mcp("kaiten_delete", kind="license", slug=v["slug"], confirm=True)
    for kind, slug in (
        ("customer", cust),
        ("entitlement", ent),
        ("entitlement_group", f"e2e-group-{RUN}"),
    ):
        with contextlib.suppress(ToolError):
            await mcp("kaiten_delete", kind=kind, slug=slug, confirm=True)


async def test_flags_are_linted_and_merged(mcp):
    flag = f"e2e-flag-{RUN}"
    try:
        r = await mcp(
            "kaiten_save_flag",
            flag=flag,
            name="E2E flag",
            rules=[
                {"name": "Premium", "rule": "__kaiten.license.slug == 'premium'", "variant": "on"}
            ],
        )
        assert (
            r["status"] == "created"
            and r["flag"]["default"] == "off"
            and r["flag"]["variants"] == {"on": True, "off": False}
        )
        assert (await mcp("kaiten_save_flag", flag=flag, enabled=True))["status"] == "unchanged"

        r = await mcp(
            "kaiten_save_flag",
            flag=flag,
            rules=[{"name": "Broken", "rule": "__kaiten.license.slug ==", "variant": "on"}],
        )
        assert r["status"] == "rejected" and r["broken_rules"][0]["rule"] == "Broken"
        assert (await mcp("kaiten_get_flags", flag=flag))["rules"][0]["name"] == "Premium"

        r = await mcp(
            "kaiten_save_flag", flag=flag, enabled=False, default={"rollout": {"on": 10, "off": 90}}
        )
        assert r["status"] == "updated" and set(r["changes"]) == {"default", "enabled"}

        with pytest.raises(ToolError, match="Unknown variant"):
            await mcp("kaiten_save_flag", flag=flag, default="maybe")

        tested = await mcp("kaiten_test_rule", rule="__kaiten.license.slug != ''")
        assert tested["valid"] is True
    finally:
        await mcp("kaiten_delete", kind="feature_flag", slug=flag, confirm=True)


async def test_releases_zones_and_access(mcp):
    # Kaiten cannot delete a service account, so every run shares one.
    zone, version, sa = f"e2e-zone-{RUN}", f"0.0.0-e2e.{RUN}", "e2e-mcp"
    release_slug = None
    try:
        r = await mcp(
            "kaiten_save_deployment_zone", deployment_zone=zone, name="E2E zone", type="staging"
        )
        assert r["status"] == "created"
        assert (await mcp("kaiten_save_deployment_zone", deployment_zone=zone, type="staging"))[
            "status"
        ] == "unchanged"

        r = await mcp(
            "kaiten_create_release",
            version=version,
            components=[f"e2e-api-{RUN}@1.0.0", {"name": f"e2e-web-{RUN}", "version": "1.0.0"}],
        )
        assert r["status"] == "created" and len(r["notes"]) == 2
        release_slug = r["release"]["slug"]
        assert (
            await mcp(
                "kaiten_create_release",
                version=version,
                components=[f"e2e-web-{RUN}@1.0.0", f"e2e-api-{RUN}@1.0.0"],
            )
        )["status"] == "unchanged"
        with pytest.raises(ToolError, match="immutable"):
            await mcp("kaiten_create_release", version=version, components=[f"e2e-api-{RUN}@2.0.0"])

        r = await mcp("kaiten_deploy_release", deployment_zone=zone, release=version)
        assert r["status"] == "updated" and r["changes"]["release"] == {"from": None, "to": version}
        assert (await mcp("kaiten_deploy_release", deployment_zone=zone, release=release_slug))[
            "status"
        ] == "unchanged"
        assert (await mcp("kaiten_get_deployments", deployment_zone=zone))["release"][
            "version"
        ] == version
        assert (await mcp("kaiten_delete", kind="release", slug=release_slug))["impact"][
            "running_in_zones"
        ] == [zone]

        r = await mcp(
            "kaiten_issue_token", service_account=sa, token_name="e2e", scopes=["read:instances"]
        )
        assert r["status"] == "created" and r["token"]["token"].startswith("ksh_")
        with pytest.raises(ToolError, match="Unknown scope"):
            await mcp(
                "kaiten_issue_token",
                service_account=sa,
                token_name="bad",
                scopes=["read:everything"],
            )
        token_slug = r["token"]["slug"]
        assert (await mcp("kaiten_revoke_token", service_account=sa, token=token_slug))[
            "status"
        ] == "deleted"
        assert all(
            t["slug"] != token_slug
            for t in (await mcp("kaiten_get_access", service_account=sa))["service_accounts"][0][
                "tokens"
            ]
        )
    finally:
        await mcp("kaiten_delete", kind="deployment_zone", slug=zone, confirm=True)
        if release_slug:
            await mcp("kaiten_delete", kind="release", slug=release_slug, confirm=True)
        for name in (f"e2e-api-{RUN}", f"e2e-web-{RUN}"):
            for hit in (await mcp("kaiten_search", query=name, kinds=["component"]))["matches"]:
                await mcp("kaiten_delete", kind="component", slug=hit["slug"], confirm=True)


async def test_metadata_schema_changes_are_dry_run(mcp):
    key = f"e2e_tier_{RUN}"
    r = await mcp(
        "kaiten_save_metadata_field",
        key=key,
        resource_type="DEPLOYMENT_ZONE",
        label="E2E tier",
        json_schema={"type": "string"},
    )
    assert r["status"] == "created"
    assert (
        await mcp(
            "kaiten_save_metadata_field", key=key, resource_type="DEPLOYMENT_ZONE", label="E2E tier"
        )
    )["status"] == "unchanged"
    r = await mcp(
        "kaiten_save_metadata_field",
        key=key,
        resource_type="DEPLOYMENT_ZONE",
        json_schema={"type": "string", "maxLength": 20},
    )
    assert r["status"] == "updated"
    r = await mcp(
        "kaiten_save_metadata_field", key=key, resource_type="DEPLOYMENT_ZONE", archived=True
    )
    assert r["status"] == "updated" and r["metadata_field"]["archived"] is True
