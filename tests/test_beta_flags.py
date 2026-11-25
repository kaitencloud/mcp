# Copyright 2026 KAITEN INC
# SPDX-License-Identifier: Apache-2.0

"""A beta's prerequisites: the three instance lists its rules read.

Driven through a real MCP client and the real SDK, over a mocked Kaiten that holds its flags and
its metadata fields, and answers a write as Kaiten does.
"""

import json

import pytest
from test_billing import api, sent
from test_metadata_fields import STAMPS
from test_tools import call, fresh_client  # noqa: F401  -- fresh_client is autouse

LISTS = ("betaAccess", "betaOptIn", "betaOptOut")
INVITED = (
    "has(__kaiten.instance) && has(__kaiten.instance.metadata.betaAccess)"
    " && 'menu-insights' in __kaiten.instance.metadata.betaAccess"
)


def beta_list(key: str, *accepted: str, schema: dict | None = None) -> dict:
    """An INSTANCE metadata field holding flag slugs, as Kaiten answers it."""
    return {
        "id": f"id-{key}",
        "key": key,
        "label": key,
        "jsonSchema": schema
        or {
            "type": "array",
            "items": {"type": "string", "enum": list(accepted)},
            "uniqueItems": True,
        },
        "resourceType": "INSTANCE",
        "displayOrder": 0,
        "createdBy": {"id": "u", "name": "u"},
        "updatedBy": {"id": "u", "name": "u"},
        **STAMPS,
    }


def flag(slug: str, *, stage: str | None = None, rules: list[dict] | None = None) -> dict:
    """A boolean flag, default off, as Kaiten answers it."""
    return {
        "id": f"id-{slug}",
        "slug": slug,
        "name": slug,
        "description": "",
        "type": "boolean",
        "enabled": True,
        "event_name": f"{slug}.evaluated",
        "metadata": {"stage": stage} if stage else {},
        "variants": [
            {"name": "on", "description": "", "value": True},
            {"name": "off", "description": "", "value": False},
        ],
        "default_variant": {"type": "basic", "value": "off"},
        "targetings": rules or [],
        **STAMPS,
    }


def kaiten_with(*fields: dict, flags: tuple[dict, ...] = ()):
    """A Kaiten holding these instance fields and flags. Every rule lints clean, as a guarded read
    of an undeclared list does on Kaiten."""
    held = {f["slug"]: f for f in flags}

    def create(r):
        body = json.loads(r.content)
        held[body["slug"]] = {**flag(body["slug"]), **body}
        return 201, held[body["slug"]]

    routes = {
        ("GET", "/metadata-fields"): lambda r: list(fields),
        ("POST", "/feature-flags"): create,
        ("POST", "/feature-flags/targeting/lint"): {"valid": True, "issues": []},
    }
    for slug in ("menu-insights", *held):
        routes[("GET", f"/feature-flags/{slug}")] = lambda r, s=slug: held.get(s)

        def put(r, s=slug):
            held[s] = {**held[s], **json.loads(r.content)}
            return 204, None

        routes[("PUT", f"/feature-flags/{slug}")] = put
    return api(routes)


def writes(seen) -> list[tuple[str, str]]:
    return [
        (r.method, r.url.path)
        for r in seen
        if r.method != "GET" and not r.url.path.endswith("/targeting/lint")
    ]


async def save_private_beta(**more):
    return await call(
        "kaiten_save_flag",
        flag="menu-insights",
        name="Menu Insights",
        metadata={"stage": "private_beta"},
        rules=[{"name": "Invited", "rule": INVITED, "variant": "on"}],
        **more,
    )


async def test_a_beta_with_no_list_declared_is_refused_with_the_calls_that_declare_them():
    seen = kaiten_with()
    out = (await save_private_beta()).structured_content
    assert out["status"] == "rejected"
    assert out["beta"]["lists"] == dict.fromkeys(LISTS, "missing")
    assert [f["arguments"]["key"] for f in out["beta"]["fix"]] == list(LISTS)
    assert out["beta"]["fix"][0] == {
        "tool": "kaiten_save_metadata_field",
        "arguments": {
            "key": "betaAccess",
            "label": "Beta access",
            "json_schema": {
                "type": "array",
                "items": {"type": "string", "enum": ["menu-insights"]},
                "uniqueItems": True,
            },
        },
    }
    assert writes(seen) == []


async def test_a_list_that_refuses_the_slug_is_fixed_by_adding_it_to_the_values_it_has():
    seen = kaiten_with(
        beta_list("betaAccess", "order-forecast"),
        beta_list("betaOptIn", "order-forecast", "menu-insights"),
        beta_list("betaOptOut", "order-forecast", "menu-insights"),
    )
    out = (await save_private_beta()).structured_content
    assert out["status"] == "rejected"
    assert out["beta"]["lists"] == {
        "betaAccess": "does not accept this flag",
        "betaOptIn": "ready",
        "betaOptOut": "ready",
    }
    (fix,) = out["beta"]["fix"]
    assert fix["arguments"] == {
        "key": "betaAccess",
        "json_schema": {
            "type": "array",
            "items": {"type": "string", "enum": ["order-forecast", "menu-insights"]},
            "uniqueItems": True,
        },
    }
    assert writes(seen) == []


async def test_a_field_of_another_type_is_named_and_has_no_fix():
    kaiten_with(
        beta_list("betaAccess", schema={"type": "string"}),
        beta_list("betaOptIn", "menu-insights"),
        beta_list("betaOptOut", "menu-insights"),
    )
    out = (await save_private_beta()).structured_content
    assert out["status"] == "rejected"
    assert out["beta"]["lists"]["betaAccess"] == "not a list"
    assert "fix" not in out["beta"]


async def test_a_beta_whose_lists_are_ready_is_created_and_says_how_to_invite():
    seen = kaiten_with(*(beta_list(key, "menu-insights") for key in LISTS))
    out = (await save_private_beta()).structured_content
    assert out["status"] == "created"
    assert out["beta"] == {
        "stage": "private_beta",
        "lists": dict.fromkeys(LISTS, "ready"),
        "ready": True,
    }
    assert "betaAccess" in out["notes"][0]
    (body,) = sent(seen, "POST", "feature-flags")
    assert body["metadata"] == {"stage": "private_beta"}


async def test_a_list_with_no_enumeration_accepts_any_flag():
    kaiten_with(
        *(beta_list(k, schema={"type": "array", "items": {"type": "string"}}) for k in LISTS)
    )
    assert (await save_private_beta()).structured_content["status"] == "created"


async def test_a_flag_with_no_stage_needs_only_the_lists_its_rules_read():
    kaiten_with(beta_list("betaAccess", "menu-insights"))
    out = await call(
        "kaiten_save_flag",
        flag="menu-insights",
        name="Menu Insights",
        rules=[{"name": "Invited", "rule": INVITED, "variant": "on"}],
    )
    assert out.structured_content["status"] == "created"
    assert out.structured_content["beta"]["lists"] == {"betaAccess": "ready"}


async def test_a_flag_that_is_not_a_beta_never_reads_the_fields():
    seen = kaiten_with()
    out = await call("kaiten_save_flag", flag="menu-insights", name="Menu Insights")
    assert out.structured_content["status"] == "created"
    assert "beta" not in out.structured_content
    assert not [r for r in seen if r.url.path.endswith("/metadata-fields")]


@pytest.mark.parametrize("change", [{"enabled": False}, {"name": "Menu insights"}])
async def test_a_change_that_leaves_the_beta_as_it_is_is_never_held_back_by_its_lists(change):
    """The kill switch above all: a beta whose lists are gone can still be turned off."""
    rules = [{"type": "basic", "name": "Invited", "rule": INVITED, "variant": "on"}]
    seen = kaiten_with(flags=(flag("menu-insights", stage="private_beta", rules=rules),))
    out = (await call("kaiten_save_flag", flag="menu-insights", **change)).structured_content
    assert out["status"] == "updated"
    assert writes(seen) == [("PUT", "/api/feature-flags/menu-insights")]


async def test_moving_a_beta_to_its_next_stage_checks_the_lists_again():
    rules = [{"type": "basic", "name": "Invited", "rule": INVITED, "variant": "on"}]
    seen = kaiten_with(
        beta_list("betaAccess", "menu-insights"),
        flags=(flag("menu-insights", stage="private_beta", rules=rules),),
    )
    out = await call("kaiten_save_flag", flag="menu-insights", metadata={"stage": "public_beta"})
    assert out.structured_content["status"] == "rejected"
    assert out.structured_content["beta"]["lists"] == {
        "betaAccess": "ready",
        "betaOptIn": "missing",
        "betaOptOut": "missing",
    }
    assert writes(seen) == []


async def test_general_availability_drops_the_stage_and_the_check():
    rules = [{"type": "basic", "name": "Invited", "rule": INVITED, "variant": "on"}]
    seen = kaiten_with(flags=(flag("menu-insights", stage="private_beta", rules=rules),))
    out = await call(
        "kaiten_save_flag",
        flag="menu-insights",
        metadata={"stage": None},
        rules=[{"name": "Everyone", "rule": "true", "variant": "on"}],
    )
    assert out.structured_content["status"] == "updated"
    assert "beta" not in out.structured_content
    assert not [r for r in seen if r.url.path.endswith("/metadata-fields")]


async def test_reading_a_beta_says_which_of_its_lists_are_not_ready():
    rules = [{"type": "basic", "name": "Invited", "rule": INVITED, "variant": "on"}]
    kaiten_with(
        beta_list("betaAccess", "menu-insights"),
        flags=(flag("menu-insights", stage="public_beta", rules=rules),),
    )
    out = (await call("kaiten_get_flags", flag="menu-insights")).structured_content
    assert out["beta"]["ready"] is False
    assert out["beta"]["lists"]["betaOptIn"] == "missing"
