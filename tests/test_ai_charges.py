# Copyright 2026 KAITEN INC
# SPDX-License-Identifier: Apache-2.0

"""Explaining, listing, refunding and pricing AI charges: real MCP and SDK over a mocked API."""

import json

import httpx
import pytest
from fastmcp import Client
from test_tools import kaiten

from kaiten_mcp import ai_pricing, sdk
from kaiten_mcp.server import build

POOL = "/api/instances/acme-prod/entitlements/ai-credits/usage"
TX = "req-42"
RULE_ID = "3a9b8c7d-0000-4000-8000-000000000001"
PRICING = {
    "ruleId": RULE_ID,
    "specificity": "EXACT_TYPED",
    "rates": {"perInputToken": "0.001", "perOutputToken": "0.003"},
    "minimumCreditsPerRequest": "0",
    "maximumCreditsPerRequest": "50",
    "chargeOnError": "NEVER",
}
# The new contract's row (lot 2 example), refunds included.
ROW = {
    "transactionId": TX,
    "status": "COMMITTED",
    "reportedAt": "2026-11-11T10:00:00Z",
    "committedAt": "2026-11-11T10:00:02Z",
    "model": "gpt-4o-2024-11-20",
    "modelSlug": "gpt-4o",
    "requestType": "COMPLETION",
    "requestStatus": "SUCCESS",
    "usage": {"inputTokens": 1000, "outputTokens": 800},
    "authorized": True,
    "conversionRuleId": RULE_ID,
    "credits": {"reserved": "3.4", "computed": "3.4", "charged": "3.4", "refunded": "1"},
    "valueBefore": 10,
    "valueAfter": 13.4,
    "windowStart": "2026-11-01T00:00:00Z",
    "pricing": PRICING,
    "licenseId": "6f1c2a9e-0000-4000-8000-000000000002",
    "licenseSlug": "pro",
    "warnings": ["MODEL_DEPRECATED"],
    "reportSeq": 17,
    "vendorCost": {"reported": "0.0125", "computed": "0.0122", "currency": "USD"},
    "refunds": [
        {
            "id": "8d0e0000-0000-4000-8000-000000000003",
            "credits": "1",
            "reason": "duplicate call",
            "reportSeq": 18,
            "createdAt": "2026-11-11T11:00:00Z",
            "createdBy": {"id": "u1", "name": "Ada Lovelace"},
        }
    ],
}
# The same operation as a server without snapshots answers it (Core #77 as first drafted).
OLD_ROW = {
    k: v
    for k, v in ROW.items()
    if k
    not in ("pricing", "licenseId", "licenseSlug", "warnings", "reportSeq", "vendorCost", "refunds")
} | {"credits": {"reserved": "3.4", "computed": "3.4", "charged": "3.4"}}


def rule(model=None, request_type=None, rid=None, **extra):
    return {
        "id": rid or f"rule-{model or 'any'}-{request_type or 'all'}",
        "licenseSlug": "pro",
        "entitlementSlug": "ai-credits",
        "modelSlug": model,
        "requestType": request_type,
        "rates": {"perRequest": "1"},
        "minimumCreditsPerRequest": "0",
        "maximumCreditsPerRequest": None,
        "chargeOnError": "NEVER",
        "status": "ACTIVE",
        "grantedByLicense": True,
        "grantedByAddons": False,
        "createdAt": "2026-11-01T00:00:00Z",
        "createdBy": {"id": "u1", "name": "Ada Lovelace"},
        "updatedAt": "2026-11-02T00:00:00Z",
        "updatedBy": {"id": "u2", "name": "Grace Hopper"},
        **extra,
    }


ENT = {
    "id": "e1",
    "slug": "ai-credits",
    "name": "AI credits",
    "type": "NUMBER_AI_CREDIT",
    "aggregationMethod": "SUM",
    "description": "Credit pool",
    "createdAt": "2026-01-01T00:00:00Z",
    "updatedAt": "2026-01-01T00:00:00Z",
}


@pytest.fixture(autouse=True)
def fresh_client():
    yield
    sdk.use(None)


async def call(tool, writes=True, **args):
    async with Client(build(writes=writes, ai_enabled=True)) as c:
        return await c.call_tool(tool, args, raise_on_error=False)


def page(*items, more=False, cursor=None):
    return {"items": list(items), "hasMore": more, **({"nextCursor": cursor} if cursor else {})}


# --- the ladder ---------------------------------------------------------------

GPT = {"slug": "gpt-4o", "status": "ACTIVE", "externalIds": ["gpt-4o-2024-11-20"]}
ALL_LEVELS = [
    rule("gpt-4o", "COMPLETION"),
    rule("gpt-4o", None),
    rule(None, "COMPLETION"),
    rule(None, None),
]


@pytest.mark.parametrize(
    "rules,model,request_type,expected",
    [
        (ALL_LEVELS, GPT, "COMPLETION", ("EXACT_TYPED", "rule-gpt-4o-COMPLETION")),
        (ALL_LEVELS[1:], GPT, "COMPLETION", ("EXACT_ALL", "rule-gpt-4o-all")),
        (ALL_LEVELS[2:], GPT, "COMPLETION", ("ANY_TYPED", "rule-any-COMPLETION")),
        (ALL_LEVELS[3:], GPT, "COMPLETION", ("ANY_ALL", "rule-any-all")),
        # Exact for every type beats a wildcard for this type.
        (
            [rule(None, "EMBEDDING"), rule("gpt-4o", None)],
            GPT,
            "EMBEDDING",
            ("EXACT_ALL", "rule-gpt-4o-all"),
        ),
        # Typed rules of another type never match.
        (
            [rule("gpt-4o", "EMBEDDING"), rule(None, "EMBEDDING")],
            GPT,
            "COMPLETION",
            ("NO_RULE", None),
        ),
        # Another model's rule never matches.
        ([rule("claude", "COMPLETION")], GPT, "COMPLETION", ("NO_RULE", None)),
        # Archived rules are ignored.
        (
            [rule("gpt-4o", "COMPLETION", status="ARCHIVED"), rule(None, None)],
            GPT,
            "COMPLETION",
            ("ANY_ALL", "rule-any-all"),
        ),
        # Archived models are never priced, not even by a wildcard.
        (ALL_LEVELS, {**GPT, "status": "ARCHIVED"}, "COMPLETION", ("MODEL_ARCHIVED", None)),
        # Unknown models are never priced, not even by a wildcard.
        (ALL_LEVELS, None, "COMPLETION", ("UNKNOWN_MODEL", None)),
        ([], GPT, "COMPLETION", ("NO_RULE", None)),
    ],
)
def test_ladder_resolution_table(rules, model, request_type, expected):
    got = ai_pricing.resolve(rules, model, request_type)
    if got["priced"]:
        assert (got["specificity"], got["rule_id"]) == expected
    else:
        assert (got["denial_reason"], None) == expected


def test_ladder_flags_deprecated_ambiguous_and_shadowed_rules():
    got = ai_pricing.resolve(
        [
            rule("gpt-4o", "COMPLETION", rid="a"),
            rule("gpt-4o", "COMPLETION", rid="b"),
            rule(None, None),
        ],
        {**GPT, "status": "DEPRECATED"},
        "COMPLETION",
    )
    assert got["rule_id"] == "a" and got["ambiguous"] == ["a", "b"]
    assert got["shadowed_rules"] == ["rule-any-all"]
    assert got["warning"] == "MODEL_DEPRECATED"


def test_model_resolves_by_slug_or_exact_external_id_only():
    models = [GPT, {"slug": "claude", "status": "ACTIVE", "externalIds": []}]
    assert ai_pricing.find_model(models, "gpt-4o") is GPT
    assert ai_pricing.find_model(models, "gpt-4o-2024-11-20") is GPT
    assert ai_pricing.find_model(models, "GPT-4O") is None
    assert ai_pricing.find_model(models, "gpt-4") is None


# --- the arithmetic -------------------------------------------------------------


def test_explain_rebuilds_the_charge_from_the_snapshot():
    out = ai_pricing.explain(ROW)
    assert out["formula"]["lines"] == [
        "inputTokens 1000 x perInputToken 0.001 = 1",
        "outputTokens 800 x perOutputToken 0.003 = 2.4",
        "sum = 3.4",
    ]
    assert out["formula"]["computed"] == "3.4" and out["formula"]["charged"] == "3.4"
    assert out["matches_kaiten"] == {"computed": True, "charged": True}
    assert out["priced_by"]["specificity"] == "EXACT_TYPED"
    assert out["credits"]["refundable"] == "2.4"
    assert out["counter"] == {"before": 10, "after": 13.4, "window_start": "2026-11-01T00:00:00Z"}
    assert out["warnings"] == [
        {"code": "MODEL_DEPRECATED", "meaning": ai_pricing.WARNING_MEANING["MODEL_DEPRECATED"]}
    ]
    assert out["refunds"][0]["createdBy"]["name"] == "Ada Lovelace"
    assert out["vendor_cost"]["reported"] == "0.0125"


@pytest.mark.parametrize(
    "rates,usage,status,policy,minimum,maximum,computed,charged",
    [
        # Cache rates fall back to the input rate.
        (
            {"perInputToken": "0.5"},
            {"cacheReadInputTokens": 2, "cacheWriteInputTokens": 2},
            "SUCCESS",
            "NEVER",
            "0",
            None,
            "2",
            "2",
        ),
        # perSecond applies to durationMs / 1000.
        ({"perSecond": "2"}, {"durationMs": 1500}, "SUCCESS", "NEVER", "0", None, "3", "3"),
        # perRequest counts once; units multiply perUnit.
        (
            {"perRequest": "1", "perUnit": "0.25"},
            {"units": "4"},
            "SUCCESS",
            "NEVER",
            "0",
            None,
            "2",
            "2",
        ),
        # The minimum lifts, the maximum caps.
        ({"perRequest": "1"}, {}, "SUCCESS", "NEVER", "5", None, "1", "5"),
        ({"perRequest": "9"}, {}, "SUCCESS", "NEVER", "0", "4", "9", "4"),
        # Nothing a rate applies to: computed is the minimum.
        ({"perInputToken": "1"}, {}, "SUCCESS", "NEVER", "0.5", None, "0.5", "0.5"),
        # Error policies.
        ({"perRequest": "1"}, {}, "PROVIDER_ERROR", "NEVER", "0", None, "1", "0"),
        ({"perRequest": "1"}, {}, "PROVIDER_ERROR", "PROVIDER_ERROR_ONLY", "0", None, "1", "1"),
        ({"perRequest": "1"}, {}, "CUSTOMER_ERROR", "PROVIDER_ERROR_ONLY", "0", None, "1", "0"),
        ({"perRequest": "1"}, {}, "CUSTOMER_ERROR", "ALWAYS", "0", None, "1", "1"),
        ({"perRequest": "1"}, {}, "NOT_SENT", "ALWAYS", "0", None, "1", "0"),
        # Half-up to six places.
        (
            {"perInputToken": "0.0000005"},
            {"inputTokens": 1},
            "SUCCESS",
            "NEVER",
            "0",
            None,
            "0.000001",
            "0.000001",
        ),
        (
            {"perInputToken": "0.0000004"},
            {"inputTokens": 1},
            "SUCCESS",
            "NEVER",
            "0",
            None,
            "0",
            "0",
        ),
    ],
)
def test_formula_follows_kaiten(rates, usage, status, policy, minimum, maximum, computed, charged):
    got = ai_pricing.formula(
        {
            "rates": rates,
            "minimumCreditsPerRequest": minimum,
            "maximumCreditsPerRequest": maximum,
            "chargeOnError": policy,
        },
        usage,
        status,
    )
    assert (got["computed"], got["charged"]) == (computed, charged)


def test_old_row_without_snapshot_is_not_guessed():
    out = ai_pricing.explain(OLD_ROW)
    assert "formula" not in out and "priced_by" not in out
    assert out["warnings"] == []
    assert "no pricing snapshot" in " ".join(out["notes"])
    assert out["credits"]["charged"] == "3.4"


def test_unknown_members_rates_and_warnings_are_tolerated():
    row = {
        **ROW,
        "somethingNew": {"x": 1},
        "warnings": ["MODEL_DEPRECATED", "BRAND_NEW_WARNING"],
        "pricing": {**PRICING, "rates": {**PRICING["rates"], "perReasoningToken": "1"}, "extra": 1},
    }
    out = ai_pricing.explain(row)
    assert out["warnings"][1]["code"] == "BRAND_NEW_WARNING"
    assert "not known" in out["warnings"][1]["meaning"]
    assert out["formula"]["incomplete"] == [
        "rate perReasoningToken is not known to this MCP version: not recomputed"
    ]


def test_unreadable_snapshot_tombstone_unauthorized_and_reservation_rows():
    unreadable = ai_pricing.explain({**OLD_ROW, "warnings": ["PRICING_SNAPSHOT_UNREADABLE"]})
    assert "could not decode" in " ".join(unreadable["notes"])
    tombstone = ai_pricing.explain(
        {
            "transactionId": TX,
            "status": "RELEASED",
            "model": "",
            "requestType": "COMPLETION",
            "usage": {},
            "credits": {},
            "warnings": ["NO_RESERVATION"],
            "refunds": [],
        }
    )
    assert "tombstone" in " ".join(tombstone["notes"])
    assert "formula" not in tombstone
    unpriced = ai_pricing.explain(
        {
            **OLD_ROW,
            "authorized": False,
            "denialReason": "UNKNOWN_MODEL",
            "warnings": ["UNKNOWN_MODEL"],
        }
    )
    assert unpriced["denial"]["reason"] == "UNKNOWN_MODEL"
    assert "No rule priced" in " ".join(unpriced["notes"])
    reserved = ai_pricing.explain(
        {
            **ROW,
            "status": "RESERVED",
            "requestStatus": None,
            "credits": {"reserved": "3.4"},
            "refunds": [],
        }
    )
    assert reserved["matches_kaiten"] == {"reserved": True}
    assert "refundable" not in reserved["credits"]


# --- explain, through MCP ----------------------------------------------------------


async def test_explain_reads_the_row_over_the_whole_history():
    seen = kaiten(lambda r: httpx.Response(200, json=page(ROW)))
    result = await call(
        "kaiten_explain_ai_charge",
        instance="acme-prod",
        entitlement="ai-credits",
        transaction_id=TX,
    )
    assert not result.is_error, result
    body = result.structured_content
    assert body["formula"]["charged"] == "3.4"
    assert body["instance"] == "acme-prod"
    assert [r.url.path for r in seen] == [f"{POOL}/inferences"]
    assert dict(seen[0].url.params) == {
        "transactionId": TX,
        "from": "1970-01-01T00:00:00Z",
        "limit": "1",
    }


async def test_explain_errors():
    kaiten(lambda r: httpx.Response(200, json=page()))
    missing = await call(
        "kaiten_explain_ai_charge",
        instance="acme-prod",
        entitlement="ai-credits",
        transaction_id=TX,
    )
    assert missing.is_error and "No AI operation" in missing.content[0].text
    kaiten(
        lambda r: httpx.Response(
            404,
            json={"status": 404, "code": "ListAiInferences.ContextNotFound", "detail": "not found"},
        )
    )
    gone = await call(
        "kaiten_explain_ai_charge",
        instance="acme-prod",
        entitlement="ai-credits",
        transaction_id=TX,
    )
    assert gone.is_error and "AI credit pool" in gone.content[0].text
    seen = kaiten(lambda r: httpx.Response(500))
    bad = await call(
        "kaiten_explain_ai_charge",
        instance="acme-prod",
        entitlement="ai-credits",
        transaction_id="a b",
    )
    assert bad.is_error and not seen


async def test_explain_an_old_server_row_says_there_is_no_snapshot():
    kaiten(lambda r: httpx.Response(200, json=page(OLD_ROW)))
    result = await call(
        "kaiten_explain_ai_charge",
        instance="acme-prod",
        entitlement="ai-credits",
        transaction_id=TX,
    )
    assert not result.is_error
    assert "no pricing snapshot" in " ".join(result.structured_content["notes"])


# --- history and the organization listing ----------------------------------------------


async def test_history_new_filters_and_rows_pass_through_whole():
    seen = kaiten(lambda r: httpx.Response(200, json=page(ROW, {**OLD_ROW, "unknownMember": 1})))
    result = await call(
        "kaiten_get_ai_history",
        instance="acme-prod",
        entitlement="ai-credits",
        authorized=False,
        model_slug="gpt-4o",
        model="gpt-4o-2024-11-20",
        request_type="COMPLETION",
        since="2026-11-01",
        until="2026-11-12T00:00:00Z",
    )
    assert not result.is_error, result
    assert result.structured_content["items"][0] == ROW
    assert result.structured_content["items"][1]["unknownMember"] == 1
    assert dict(seen[-1].url.params) == {
        "from": "2026-11-01T00:00:00Z",
        "to": "2026-11-12T00:00:00Z",
        "authorized": "false",
        "modelSlug": "gpt-4o",
        "model": "gpt-4o-2024-11-20",
        "requestType": "COMPLETION",
        "limit": "50",
    }
    refused = await call(
        "kaiten_get_ai_history",
        instance="acme-prod",
        entitlement="ai-credits",
        open_reservations=True,
        authorized=True,
    )
    assert refused.is_error


async def test_organization_listing_maps_filters():
    org_row = {**ROW, "instanceSlug": "acme-prod", "entitlementSlug": "ai-credits"}
    seen = kaiten(lambda r: httpx.Response(200, json=page(org_row, more=True, cursor="c2")))
    result = await call(
        "kaiten_list_ai_inferences", instance="acme-prod", status="COMMITTED", cursor="c1", limit=10
    )
    assert result.structured_content["items"] == [org_row]
    assert seen[-1].url.path == "/api/usage/inferences"
    assert dict(seen[-1].url.params) == {
        "status": "COMMITTED",
        "instanceSlug": "acme-prod",
        "cursor": "c1",
        "limit": "10",
    }


def denied(model, reason, instance="acme-prod", at="2026-11-10T00:00:00Z"):
    return {
        "transactionId": f"t-{model}-{at}",
        "status": "COMMITTED",
        "reportedAt": at,
        "model": model,
        "requestType": "COMPLETION",
        "usage": {},
        "credits": {"charged": "0"},
        "authorized": False,
        "denialReason": reason,
        "warnings": [],
        "refunds": [],
        "instanceSlug": instance,
        "entitlementSlug": "ai-credits",
    }


async def test_unpriced_usage_groups_by_model_and_reason_and_says_when_truncated():
    pages = {
        None: page(
            denied("gpt-5", "UNKNOWN_MODEL"),
            denied("gpt-5", "UNKNOWN_MODEL", "beta-prod", "2026-11-09T00:00:00Z"),
            more=True,
            cursor="p2",
        ),
        "p2": page(denied("old-model", "MODEL_ARCHIVED"), more=True, cursor="p3"),
    }
    seen = kaiten(lambda r: httpx.Response(200, json=pages[r.url.params.get("cursor")]))
    result = await call("kaiten_list_ai_inferences", unpriced=True, scan_limit=3)
    body = result.structured_content
    assert body["scanned"] == 3 and body["truncated"] is True
    assert body["notes"][0].startswith("Truncated: 3 operations read")
    gpt5, old = body["groups"]
    assert gpt5["model"] == "gpt-5" and gpt5["operations"] == 2
    assert gpt5["instances"] == ["acme-prod", "beta-prod"]
    assert gpt5["first_seen"] == "2026-11-09T00:00:00Z"
    assert old["denial_reason"] == "MODEL_ARCHIVED"
    assert all(r.url.params["authorized"] == "false" for r in seen)
    assert [r.url.params.get("limit") for r in seen] == ["3", "1"]


async def test_unpriced_usage_complete_and_refusals():
    kaiten(lambda r: httpx.Response(200, json=page(denied("gpt-5", "NO_RULE"))))
    done = await call("kaiten_list_ai_inferences", unpriced=True)
    assert "truncated" not in done.structured_content
    assert done.structured_content["groups"][0]["meaning"] == ai_pricing.DENIAL_MEANING["NO_RULE"]
    assert (await call("kaiten_list_ai_inferences", unpriced=True, authorized=True)).is_error


async def test_organization_listing_on_an_older_server():
    kaiten(lambda r: httpx.Response(404, json={"status": 404, "detail": "no route"}))
    result = await call("kaiten_list_ai_inferences")
    assert result.is_error and "kaiten_get_ai_history" in result.content[0].text


# --- rate card -------------------------------------------------------------------------


def rate_card_api(models_by_slug=None, model_list=None, rules=None):
    def handler(r):
        path = r.url.path
        if path == "/api/licenses/pro/conversion-rules":
            return httpx.Response(200, json=ALL_LEVELS if rules is None else rules)
        if path.startswith("/api/models/"):
            found = (models_by_slug or {}).get(path.rsplit("/", 1)[1])
            return (
                httpx.Response(200, json=found)
                if found
                else httpx.Response(404, json={"status": 404})
            )
        if path == "/api/models":
            return httpx.Response(200, json=page(*(model_list or [])))
        return httpx.Response(500)

    return kaiten(handler)


async def test_rate_card_orders_rules_by_ladder_and_reads_no_model_without_one():
    seen = rate_card_api(rules=list(reversed(ALL_LEVELS)))
    result = await call("kaiten_get_ai_rate_card", license="pro", entitlement="ai-credits")
    body = result.structured_content
    assert [r["level"] for r in body["rules"]] == list(ai_pricing.LEVELS)
    assert body["rules"][3]["model"] == "any" and body["rules"][3]["request_type"] == "any"
    assert body["rules"][0]["updated_by"] == "Grace Hopper"
    assert body["rules"][0]["granted_by_addons"] is False
    assert any("including models added to the catalogue later" in n for n in body["notes"])
    assert "resolution" not in body
    assert dict(seen[0].url.params) == {"status": "ACTIVE", "entitlementSlug": "ai-credits"}
    assert len(seen) == 1


async def test_rate_card_resolves_a_slug_for_every_request_type():
    rate_card_api(
        models_by_slug={"gpt-4o": GPT}, rules=[rule("gpt-4o", "COMPLETION"), rule(None, None)]
    )
    result = await call(
        "kaiten_get_ai_rate_card", license="pro", entitlement="ai-credits", model="gpt-4o"
    )
    res = result.structured_content["resolution"]
    assert res["catalogue_model"] == {"slug": "gpt-4o", "status": "ACTIVE"}
    assert set(res["by_request_type"]) == set(ai_pricing.REQUEST_TYPES)
    assert res["by_request_type"]["COMPLETION"]["specificity"] == "EXACT_TYPED"
    assert res["by_request_type"]["EMBEDDING"]["specificity"] == "ANY_ALL"
    assert res["computed"] == ai_pricing.CLIENT_SIDE


async def test_rate_card_resolves_an_external_id_and_refuses_unknown_and_archived():
    seen = rate_card_api(model_list=[GPT])
    alias = await call(
        "kaiten_get_ai_rate_card",
        license="pro",
        entitlement="ai-credits",
        model="gpt-4o-2024-11-20",
        request_type="COMPLETION",
    )
    got = alias.structured_content["resolution"]
    assert got["catalogue_model"]["slug"] == "gpt-4o"
    assert got["by_request_type"] == {
        "COMPLETION": ai_pricing.resolve(ALL_LEVELS, GPT, "COMPLETION")
    }
    assert [r.url.path for r in seen][-2:] == ["/api/models/gpt-4o-2024-11-20", "/api/models"]
    unknown = await call(
        "kaiten_get_ai_rate_card",
        license="pro",
        entitlement="ai-credits",
        model="Some/Model",
        request_type="AUDIO",
    )
    assert unknown.structured_content["resolution"]["catalogue_model"] is None
    assert unknown.structured_content["resolution"]["by_request_type"]["AUDIO"] == {
        "priced": False,
        "denial_reason": "UNKNOWN_MODEL",
    }
    rate_card_api(models_by_slug={"gpt-4o": {**GPT, "status": "ARCHIVED"}})
    archived = await call(
        "kaiten_get_ai_rate_card",
        license="pro",
        entitlement="ai-credits",
        model="gpt-4o",
        request_type="VIDEO",
    )
    assert (
        archived.structured_content["resolution"]["by_request_type"]["VIDEO"]["denial_reason"]
        == "MODEL_ARCHIVED"
    )


async def test_rate_card_without_rules_or_grant():
    rate_card_api(rules=[])
    empty = await call("kaiten_get_ai_rate_card", license="pro", entitlement="ai-credits")
    assert "NO_RULE" in empty.structured_content["notes"][0]
    rate_card_api(rules=[rule("gpt-4o", None, grantedByLicense=False)])
    ungranted = await call("kaiten_get_ai_rate_card", license="pro", entitlement="ai-credits")
    assert any("Neither this licence" in n for n in ungranted.structured_content["notes"])


# --- usage reports: confirm and vendor cost ------------------------------------------------


REPORT = dict(
    instance="acme-prod",
    entitlement="ai-credits",
    transaction_id="call-001",
    phase="commit",
    request_status="SUCCESS",
    model="gpt-4o",
    request_type="COMPLETION",
    usage={"inputTokens": 10},
)


@pytest.mark.parametrize("phase", ["reserve", "commit"])
async def test_reports_need_confirm_and_preview_sends_nothing(phase):
    seen = kaiten(lambda r: httpx.Response(200, json=ENT))
    args = {**REPORT, "phase": phase}
    if phase == "reserve":
        args.pop("request_status")
    result = await call("kaiten_report_ai_usage", **args)
    body = result.structured_content
    assert body["status"] == "preview"
    assert body["usage"]["request"]["value"]["phase"] == phase
    assert "confirm=true" in body["notes"][0]
    assert [r.method for r in seen] == ["GET"]


async def test_vendor_cost_is_sent_byte_for_byte_and_a_replay_is_reported():
    def handler(r):
        if r.method == "GET":
            return httpx.Response(200, json=ENT)
        return httpx.Response(
            200,
            json={"ai": {"status": "COMMITTED", "rule": {"id": RULE_ID, "specificity": "ANY_ALL"}}},
            headers={"Idempotent-Replayed": "true"},
        )

    seen = kaiten(handler)
    result = await call("kaiten_report_ai_usage", **REPORT, vendor_cost="0.010", confirm=True)
    assert not result.is_error, result
    assert json.loads(seen[-1].content)["value"]["vendorCost"] == "0.010"
    assert result.structured_content["replayed"] is True
    assert result.structured_content["usage"]["ai"]["rule"]["specificity"] == "ANY_ALL"


@pytest.mark.parametrize(
    "patch",
    [
        {"phase": "reserve", "request_status": None, "vendor_cost": "0.01"},
        {"request_status": "NOT_SENT", "vendor_cost": "0.01"},
        {"vendor_cost": "1e-2"},
        {"vendor_cost": "0.0000000000001"},
    ],
)
async def test_vendor_cost_is_refused_where_kaiten_refuses_it(patch):
    seen = kaiten(lambda r: httpx.Response(200, json=ENT))
    args = {k: v for k, v in {**REPORT, **patch, "confirm": True}.items() if v is not None}
    result = await call("kaiten_report_ai_usage", **args)
    assert result.is_error
    assert not [r for r in seen if r.method == "POST"]


# --- refunds ---------------------------------------------------------------------------------


REFUND = dict(
    instance="acme-prod",
    entitlement="ai-credits",
    transaction_id=TX,
    idempotency_key="refund-req-42-1",
)


async def test_refund_preview_shows_what_is_left_and_sends_nothing():
    seen = kaiten(lambda r: httpx.Response(200, json=page(ROW)))
    result = await call(
        "kaiten_refund_ai_inference", **REFUND, credits="2", reason="duplicate call"
    )
    body = result.structured_content
    assert body["status"] == "preview"
    assert body["refund"]["refundable"] == "2.4"
    assert body["refund"]["would_refund"] == "2" and body["refund"]["left_after"] == "0.4"
    assert all(r.method == "GET" for r in seen)


@pytest.mark.parametrize(
    "row,credits,code",
    [
        ({**ROW, "status": "RESERVED"}, None, "NotCommitted"),
        ({**ROW, "status": "RELEASED"}, None, "NotCommitted"),
        ({**ROW, "credits": {"charged": "3.4", "refunded": "3.4"}}, None, "NothingToRefund"),
        ({**ROW, "credits": {"charged": "0"}}, None, "NothingToRefund"),
        (ROW, "2.5", "ExceedsRefundable"),
    ],
)
async def test_refund_preview_rejects_what_kaiten_would_refuse(row, credits, code):
    seen = kaiten(lambda r: httpx.Response(200, json=page(row)))
    args = {**REFUND, **({"credits": credits} if credits else {})}
    result = await call("kaiten_refund_ai_inference", **args)
    assert result.structured_content["status"] == "rejected"
    assert code in result.structured_content["reason"]
    assert all(r.method == "GET" for r in seen)


async def test_confirmed_refund_sends_the_key_and_answers_what_is_left():
    after = {**ROW, "credits": {**ROW["credits"], "refunded": "3"}}
    refund = {
        "id": "r2",
        "credits": "2",
        "reason": "duplicate call",
        "reportSeq": 19,
        "createdAt": "2026-11-11T12:00:00Z",
        "createdBy": {"id": "u1", "name": "Ada Lovelace"},
    }

    def handler(r):
        if r.method == "POST":
            return httpx.Response(200, json=refund)
        return httpx.Response(200, json=page(after))

    seen = kaiten(handler)
    result = await call(
        "kaiten_refund_ai_inference", **REFUND, credits="2", reason="duplicate call", confirm=True
    )
    assert not result.is_error, result
    post = next(r for r in seen if r.method == "POST")
    assert post.url.path == f"{POOL}/inferences/{TX}/refund"
    assert post.headers["Idempotency-Key"] == "refund-req-42-1"
    assert json.loads(post.content) == {"credits": "2", "reason": "duplicate call"}
    body = result.structured_content
    assert body["status"] == "recorded" and body["refund"] == refund
    assert body["inference"]["refundable"] == "0.4"
    assert "replayed" not in body
    # The key rides on that request only.
    assert "Idempotency-Key" not in seen[-1].headers


async def test_full_refund_sends_an_empty_body_and_reports_a_replay():
    def handler(r):
        if r.method == "POST":
            return httpx.Response(
                200,
                json={
                    "id": "r1",
                    "credits": "2.4",
                    "reportSeq": 19,
                    "createdAt": "x",
                    "createdBy": {"id": "u"},
                },
                headers={"Idempotent-Replayed": "true"},
            )
        return httpx.Response(
            200, json=page({**ROW, "credits": {"charged": "3.4", "refunded": "3.4"}})
        )

    seen = kaiten(handler)
    result = await call("kaiten_refund_ai_inference", **REFUND, confirm=True)
    assert json.loads(next(r for r in seen if r.method == "POST").content) == {}
    assert result.structured_content["replayed"] is True
    assert result.structured_content["inference"]["refundable"] == "0"


@pytest.mark.parametrize(
    "status,code",
    [
        (409, "RefundAiInference.ExceedsRefundable"),
        (409, "RefundAiInference.IdempotencyKeyReused"),
        (409, "RefundAiInference.PoolNotGranted"),
        (403, "AIUsage.Disabled"),
        (404, "RefundAiInference.InferenceNotFound"),
        (422, "RefundAiInference.InvalidReason"),
    ],
)
async def test_refund_errors_reach_the_agent_with_their_code(status, code):
    kaiten(
        lambda r: httpx.Response(status, json={"status": status, "code": code, "detail": "refused"})
    )
    result = await call("kaiten_refund_ai_inference", **REFUND, confirm=True)
    assert result.is_error and code in result.content[0].text


async def test_refund_on_a_server_without_refunds():
    kaiten(lambda r: httpx.Response(404, json={"status": 404, "detail": "no route"}))
    result = await call("kaiten_refund_ai_inference", **REFUND, confirm=True)
    assert result.is_error and "does not serve AI refunds" in result.content[0].text


@pytest.mark.parametrize(
    "patch",
    [
        {"credits": "0"},
        {"credits": "-1"},
        {"credits": "1.0000001"},
        {"credits": 1},
        {"reason": " "},
        {"reason": "x" * 501},
        {"idempotency_key": "has space"},
        {"idempotency_key": ""},
    ],
)
async def test_invalid_refund_never_reaches_transport(patch):
    seen = kaiten(lambda r: httpx.Response(500))
    result = await call("kaiten_refund_ai_inference", **{**REFUND, **patch, "confirm": True})
    assert result.is_error
    assert not seen


# --- archiving a rule -------------------------------------------------------------------------


def archive_api(target, others):
    state = dict(target)

    def handler(r):
        if r.method == "DELETE":
            state["status"] = "ARCHIVED"
            return httpx.Response(204)
        if r.url.path.endswith("/conversion-rules"):
            return httpx.Response(200, json=[target, *others])
        return httpx.Response(200, json=state)

    return kaiten(handler)


async def test_archive_preview_names_the_rule_that_takes_over():
    target = rule("gpt-4o", "COMPLETION", rid="11111111-1111-1111-1111-111111111111")
    seen = archive_api(
        target, [rule("gpt-4o", None), rule(None, None), rule("claude", "COMPLETION")]
    )
    result = await call("kaiten_archive_ai_rule", license="pro", rule_id=target["id"])
    body = result.structured_content
    assert body["status"] == "preview" and body["rule"]["level"] == "EXACT_TYPED"
    assert body["instead"]["rule_id"] == "rule-gpt-4o-all"
    assert all(r.method == "GET" for r in seen)


async def test_archive_preview_of_a_wildcard_says_its_calls_become_unpriced():
    target = rule(None, None, rid="11111111-1111-1111-1111-111111111111")
    archive_api(target, [rule("gpt-4o", "COMPLETION"), rule(None, "EMBEDDING")])
    result = await call("kaiten_archive_ai_rule", license="pro", rule_id=target["id"])
    assert result.structured_content["instead"] == {"priced": False, "denial_reason": "NO_RULE"}


async def test_confirmed_archive_deletes_then_reads_back():
    target = rule("gpt-4o", "COMPLETION", rid="11111111-1111-1111-1111-111111111111")
    seen = archive_api(target, [])
    result = await call("kaiten_archive_ai_rule", license="pro", rule_id=target["id"], confirm=True)
    body = result.structured_content
    assert body["status"] == "updated"
    assert body["changes"] == {"status": {"from": "ACTIVE", "to": "ARCHIVED"}}
    assert [r.method for r in seen] == ["GET", "DELETE", "GET"]
    assert seen[1].url.path == f"/api/licenses/pro/conversion-rules/{target['id']}"


async def test_archive_of_an_archived_rule_writes_nothing_and_bad_ids_are_refused():
    target = rule(
        "gpt-4o", "COMPLETION", rid="11111111-1111-1111-1111-111111111111", status="ARCHIVED"
    )
    seen = archive_api(target, [])
    result = await call("kaiten_archive_ai_rule", license="pro", rule_id=target["id"], confirm=True)
    assert result.structured_content["status"] == "unchanged"
    assert [r.method for r in seen] == ["GET"]
    seen = kaiten(lambda r: httpx.Response(500))
    assert (await call("kaiten_archive_ai_rule", license="pro", rule_id="not-a-uuid")).is_error
    assert not seen


# --- annotations ------------------------------------------------------------------------------


async def test_annotations_and_read_only_mode():
    async with Client(build(writes=True, ai_enabled=True)) as client:
        tools = {t.name: t for t in await client.list_tools()}
    for name in (
        "kaiten_explain_ai_charge",
        "kaiten_list_ai_inferences",
        "kaiten_get_ai_rate_card",
    ):
        assert tools[name].annotations.read_only_hint is True
    for name in ("kaiten_refund_ai_inference", "kaiten_archive_ai_rule"):
        assert tools[name].annotations.destructive_hint is True
        assert "confirm" in tools[name].input_schema["properties"]
    assert "confirm" in tools["kaiten_report_ai_usage"].input_schema["properties"]
    async with Client(build(writes=False, ai_enabled=True)) as client:
        names = {t.name for t in await client.list_tools()}
    assert {
        "kaiten_explain_ai_charge",
        "kaiten_list_ai_inferences",
        "kaiten_get_ai_rate_card",
    } <= names
    assert not names & {
        "kaiten_refund_ai_inference",
        "kaiten_archive_ai_rule",
        "kaiten_report_ai_usage",
    }


async def test_unpriced_usage_skips_rows_an_older_server_did_not_filter():
    kaiten(lambda r: httpx.Response(200, json=page(ROW, denied("gpt-5", "UNKNOWN_MODEL"))))
    result = await call("kaiten_list_ai_inferences", unpriced=True)
    body = result.structured_content
    assert [g["model"] for g in body["groups"]] == ["gpt-5"]
    assert body["notes"][0].startswith("1 operations read were not refused ones")
