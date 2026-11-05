# Copyright 2026 KAITEN INC
# SPDX-License-Identifier: Apache-2.0

"""How a plan is sold: each version's commercial terms, and the plan's own settings.

Driven through a real MCP client and the real SDK, over a mocked Kaiten whose answers are whole.
"""

import json

import pytest
from test_billing import api, capabilities, problem, sent
from test_tools import call, fresh_client  # noqa: F401  -- fresh_client is autouse

STAMPS = {"createdAt": "2026-11-01T00:00:00Z", "updatedAt": "2026-11-01T00:00:00Z"}


def version(slug: str, **extra) -> dict:
    """A License version of the growth plan, as GET /licenses/{slug} answers it."""
    return {
        "id": f"id-{slug}",
        "slug": slug,
        "name": "Cloud Growth",
        "description": "",
        "type": "PAID",
        "version": "1",
        "versionName": "v1",
        "isDefault": False,
        "familyId": "f1",
        "lifecycleState": "PUBLISHED",
        "pricingType": "CUSTOM",
        "trialPeriodDays": None,
        "requiresPaymentMethod": False,
        "selfServeCtaUrl": None,
        **STAMPS,
        **extra,
    }


def family(*versions: dict, **extra) -> dict:
    """The growth plan, as GET /license-families/growth?include=versions answers it."""
    default = next((v for v in versions if v["isDefault"]), None)
    return {
        "id": "f1",
        "slug": "growth",
        "versionCount": len(versions),
        "isPublic": False,
        "fallbackLicenseFamilySlug": None,
        "trialEndWithoutPaymentMethod": "CANCEL",
        "trialReminderDays": 3,
        **({"currentVersion": default} if default else {}),
        "versions": list(versions),
        **STAMPS,
        **extra,
    }


SOLD = {"pricingType": "PAID", "requiresPaymentMethod": True, "trialPeriodDays": 14}


async def test_a_new_version_is_sold_as_the_version_it_comes_from():
    # Left to the API's default, a new version would be CUSTOM: a plan whose default it became would stop
    # selling at checkout, and a plan another one falls back to refuses it.
    v1 = version("growth", isDefault=True, **SOLD)

    def create(r):
        body = json.loads(r.content)
        return 201, version("growth-v2", version="2", **{k: body.get(k) for k in SOLD})

    seen = api(
        {
            ("GET", "/license-families/growth"): family(v1),
            ("POST", "/licenses"): create,
            ("GET", "/licenses/growth/entitlements"): [],
            ("GET", "/licenses/growth-v2/entitlements"): [],
            ("GET", "/entitlements"): [],
        }
    )
    out = (await call("kaiten_save_license", new_version_of="growth")).structured_content
    assert sent(seen, "POST", "licenses")[-1] == {
        "familySlug": "growth",
        "name": "Cloud Growth",
        "description": "",
        "type": "PAID",
        "isDefault": False,
        **SOLD,
    }
    lic = out["license"]
    assert (lic["pricing"], lic["requires_payment_method"], lic["trial_days"]) == ("PAID", True, 14)
    assert "how it is sold copied from growth" in out["notes"]
    assert any(n.startswith("it has no price yet") for n in out["notes"])

    # What is passed wins, and a 0-day trial is none: the API refuses 0 on a new version.
    await call("kaiten_save_license", new_version_of="growth", pricing="FREE", trial_days=0)
    body = sent(seen, "POST", "licenses")[-1]
    assert (body["pricingType"], body["requiresPaymentMethod"]) == ("FREE", True)
    assert "trialPeriodDays" not in body


async def test_a_new_plan_is_sold_as_asked_and_custom_otherwise():
    def create(r):
        body = json.loads(r.content)
        return 201, version("starter", pricingType=body.get("pricingType", "CUSTOM"))

    seen = api(
        {
            ("POST", "/licenses"): create,
            ("GET", "/licenses/starter/entitlements"): [],
            ("GET", "/entitlements"): [],
        }
    )
    free = await call(
        "kaiten_save_license", name="Starter", type="COMMUNITY", slug="starter", pricing="FREE"
    )
    assert sent(seen, "POST", "licenses")[-1]["pricingType"] == "FREE"
    assert free.structured_content["license"]["pricing"] == "FREE"
    assert free.structured_content["notes"][0].startswith("it has no price yet")

    custom = await call("kaiten_save_license", name="Starter", type="COMMUNITY", slug="starter")
    assert "pricingType" not in sent(seen, "POST", "licenses")[-1]
    assert "notes" not in custom.structured_content


async def test_a_version_changes_how_it_is_sold_in_place_and_reports_it():
    state = version("growth", isDefault=True)

    def replace(r):
        state.update(json.loads(r.content))
        return 204, None

    seen = api(
        {
            ("GET", "/licenses/growth"): lambda r: state,
            ("PUT", "/licenses/growth"): replace,
            ("GET", "/licenses/growth/entitlements"): [],
            ("GET", "/entitlements"): [],
            ("GET", "/license-families"): [family(state)],
            ("GET", "/instances"): [],
        }
    )
    out = (
        await call(
            "kaiten_save_license", license="growth", pricing="PAID", requires_payment_method=True
        )
    ).structured_content
    assert out["status"] == "updated"
    assert out["changes"] == {
        "pricing": {"from": "CUSTOM", "to": "PAID"},
        "requires_payment_method": {"from": False, "to": True},
    }
    # The full replacement the SDK sends keeps the default, and carries only the terms that change.
    assert sent(seen, "PUT", "licenses/growth") == [
        {
            "name": "Cloud Growth",
            "description": "",
            "type": "PAID",
            "isDefault": True,
            "versionName": "v1",
            "pricingType": "PAID",
            "requiresPaymentMethod": True,
        }
    ]

    # Asking for what is held writes nothing: no trial is a 0-day trial, no URL an empty one.
    same = await call(
        "kaiten_save_license", license="growth", pricing="PAID", trial_days=0, self_serve_cta_url=""
    )
    assert same.structured_content["status"] == "unchanged"
    assert len(sent(seen, "PUT", "licenses/growth")) == 1


def a_plan(state: dict):
    """The growth plan at state, whose PATCH merges what it is sent and answers as Kaiten does,
    without the versions."""

    def patch(r):
        body = json.loads(r.content)
        if "fallbackLicenseFamilySlug" in body:
            body["fallbackLicenseFamilySlug"] = body["fallbackLicenseFamilySlug"] or None
        state.update(body)
        return {k: v for k, v in state.items() if k != "versions"}

    return api(
        {
            ("GET", "/license-families/growth"): lambda r: state,
            ("PATCH", "/license-families/growth"): patch,
        }
    )


async def test_plan_settings_send_only_what_differs():
    state = family(version("growth", isDefault=True, **SOLD))
    seen = a_plan(state)
    out = (
        await call(
            "kaiten_save_plan_settings",
            plan="growth",
            public=True,
            fallback_plan="starter",
            trial_reminder_days=3,
        )
    ).structured_content
    assert out["status"] == "updated"
    assert out["changes"] == {
        "public": {"from": False, "to": True},
        "fallback_plan": {"from": None, "to": "starter"},
    }
    # The reminder is already 3 days, and a fallback restated would be checked again: neither is sent.
    assert sent(seen, "PATCH", "license-families/growth") == [
        {"isPublic": True, "fallbackLicenseFamilySlug": "starter"}
    ]
    assert out["plan_settings"]["listed_version"] == "growth"
    assert "notes" not in out

    same = await call("kaiten_save_plan_settings", plan="growth", public=True)
    assert same.structured_content["status"] == "unchanged"
    assert len(sent(seen, "PATCH", "license-families/growth")) == 1

    # "" removes the fallback.
    out = (
        await call("kaiten_save_plan_settings", plan="growth", fallback_plan="")
    ).structured_content
    assert sent(seen, "PATCH", "license-families/growth")[-1] == {"fallbackLicenseFamilySlug": ""}
    assert out["changes"] == {"fallback_plan": {"from": "starter", "to": None}}


@pytest.mark.parametrize(
    ("versions", "listed", "note"),
    [
        ([version("growth", **SOLD)], None, "not listed yet"),
        ([version("growth", lifecycleState="DRAFT", **SOLD)], None, "not listed yet"),
        ([version("growth", isDefault=True)], "growth", "growth is CUSTOM"),
    ],
    ids=["no default version", "nothing published", "sold on request"],
)
async def test_a_plan_made_public_says_what_its_catalogue_lists(versions, listed, note):
    a_plan(family(*versions))
    out = (await call("kaiten_save_plan_settings", plan="growth", public=True)).structured_content
    assert out["plan_settings"].get("listed_version") == listed
    assert out["notes"][0].startswith(note)


async def test_plan_settings_where_billing_is_off_are_explained():
    seen = api(
        {
            ("GET", "/license-families/growth"): family(version("growth", isDefault=True)),
            ("PATCH", "/license-families/growth"): problem(
                403, "Billing.Disabled", "billing is off"
            ),
            ("GET", "/billing/capabilities"): capabilities(
                enabled=False, disabledReason="DEPLOYMENT_DISABLED"
            ),
        }
    )
    refused = await call("kaiten_save_plan_settings", plan="growth", public=True)
    assert refused.is_error
    assert "KAITEN_BILLING_ENABLED" in refused.content[0].text
    assert refused.content[0].text.endswith("(Billing.Disabled)")
    assert len(sent(seen, "PATCH", "license-families/growth")) == 1


async def test_the_catalog_shows_how_each_plan_is_sold():
    v1 = version("growth", isDefault=True, selfServeCtaUrl="https://example.com/sales", **SOLD)
    growth = family(v1, isPublic=True, fallbackLicenseFamilySlug="starter")
    api(
        {
            ("GET", "/instances"): [],
            ("GET", "/license-families/growth"): growth,
            ("GET", "/licenses/growth/entitlements"): [],
            ("GET", "/entitlements"): [],
            ("GET", "/entitlement-groups"): [],
            ("GET", "/license-families"): [growth],
        }
    )
    one = (await call("kaiten_get_catalog", plan="growth")).structured_content
    assert {k: one[k] for k in ("plan", "public", "fallback_plan", "trial_reminder_days")} == {
        "plan": "growth",
        "public": True,
        "fallback_plan": "starter",
        "trial_reminder_days": 3,
    }
    sold = {k: one["versions"][0][k] for k in ("pricing", "requires_payment_method", "trial_days")}
    assert sold == {"pricing": "PAID", "requires_payment_method": True, "trial_days": 14}
    assert one["versions"][0]["self_serve_cta_url"] == "https://example.com/sales"

    plans = (await call("kaiten_get_catalog")).structured_content["plans"]
    assert {k: plans[0][k] for k in ("plan", "public", "fallback_plan")} == {
        "plan": "growth",
        "public": True,
        "fallback_plan": "starter",
    }


@pytest.mark.parametrize(
    ("action", "default", "note"),
    [
        ("make_default", False, "growth is public: its public catalogue now offers growth-v2"),
        ("unset_default", True, "growth is public, but its public catalogue lists a default"),
    ],
)
async def test_moving_the_default_of_a_public_plan_says_what_its_catalogue_offers(
    action, default, note
):
    state = version("growth-v2", version="2", isDefault=default)

    def replace(r):
        state.update(json.loads(r.content))
        return 204, None

    api(
        {
            ("GET", "/licenses/growth-v2"): lambda r: state,
            ("PUT", "/licenses/growth-v2"): replace,
            ("GET", "/license-families"): [family(isPublic=True)],
        }
    )
    out = (
        await call("kaiten_set_license_state", license="growth-v2", action=action)
    ).structured_content
    assert out["status"] == "updated"
    assert out["notes"][0].startswith(note)
