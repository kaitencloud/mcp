# Copyright 2026 KAITEN INC
# SPDX-License-Identifier: Apache-2.0

"""AI intents through real MCP and SDK, with no network/provider calls."""

import json

import httpx
import pytest
from fastmcp import Client
from test_tools import kaiten

from kaiten_mcp import sdk
from kaiten_mcp.server import build
from kaiten_mcp.tools.ai import decimal_string, rates_in, usage_in


async def call(tool, **args):
    async with Client(build(writes=True, ai_enabled=True)) as c:
        return await c.call_tool(tool, args, raise_on_error=False)


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
RULE = {
    "id": "11111111-1111-1111-1111-111111111111",
    "licenseSlug": "premium-v1",
    "entitlementSlug": "ai-credits",
    "modelSlug": "assistant",
    "requestType": "COMPLETION",
    "rates": {"perInputToken": "0.0000000001"},
    "minimumCreditsPerRequest": "0.000001",
    "maximumCreditsPerRequest": "1.000001",
    "chargeOnError": "NEVER",
    "status": "ACTIVE",
    "grantedByLicense": True,
}
MODEL = {
    "slug": "assistant",
    "name": "Assistant",
    "vendor": "own",
    "modality": "TEXT",
    "status": "ACTIVE",
    "externalIds": ["provider/assistant-2026"],
    "vendorCosts": {"currency": "USD", "perInputToken": "0.000000000001"},
    "metadata": {"owner": "team"},
}


@pytest.fixture(autouse=True)
def fresh_client():
    yield
    sdk.use(None)


@pytest.mark.parametrize(
    "phase,status", [("reserve", None), ("commit", "SUCCESS"), ("commit", "NOT_SENT")]
)
async def test_ai_phase_retries_keep_identity_and_exact_body(phase, status):
    bodies = []
    outcome = {
        "value": {"type": "number", "value": 0.000001},
        "ai": {
            "transactionId": "call-001",
            "phase": phase,
            "credits": {"charged": "999999999999.999999"},
            "vendorCost": {"amount": "0.000000000001", "currency": "USD"},
            "remaining": "1.000001",
        },
    }

    def handler(r):
        if r.method == "GET":
            return httpx.Response(200, json=ENT)
        bodies.append(json.loads(r.content))
        return httpx.Response(200, json=outcome)

    seen = kaiten(handler)
    args = dict(
        instance="acme-prod",
        entitlement="ai-credits",
        transaction_id="call-001",
        phase=phase,
        model="provider/assistant-2026",
        request_type="COMPLETION",
        usage={"inputTokens": 100, "outputTokens": 50},
        metadata={"trace": {"provider": "own", "ordinal": 1}},
    )
    if status:
        args["request_status"] = status
    for _ in range(2):
        result = await call("kaiten_report_ai_usage", **args)
        assert not result.is_error, result
        assert result.structured_content["usage"] == outcome
    assert bodies[0] == bodies[1]
    assert bodies[0]["value"]["transactionId"] == "call-001"
    assert bodies[0]["value"]["type"] == "ai_usage"
    assert bodies[0]["metadata"] == args["metadata"]
    assert all("Idempotency-Key" not in r.headers for r in seen)
    assert [r.url.path for r in seen if r.method == "POST"] == [
        "/api/instances/acme-prod/entitlements/ai-credits/usage"
    ] * 2


@pytest.mark.parametrize(
    "patch",
    [
        {"transaction_id": " "},
        {"usage": {"inputTokens": True}},
        {"usage": {"durationMs": -1}},
        {"usage": {"units": 0.000001}},
        {"usage": {"units": "1e-6"}},
        {"usage": {"surprise": 1}},
        {"request_status": "SUCCESS"},
        {"phase": "commit"},
        {"phase": "commit", "request_status": "SUCCESS", "ttl_seconds": 5},
    ],
)
async def test_bad_ai_report_never_reaches_transport(patch):
    seen = kaiten(lambda r: httpx.Response(500))
    result = await call(
        "kaiten_report_ai_usage",
        **{
            **dict(
                instance="acme-prod",
                entitlement="ai-credits",
                transaction_id="call-001",
                phase="reserve",
                model="assistant",
                request_type="COMPLETION",
                usage={"inputTokens": 1},
            ),
            **patch,
        },
    )
    assert result.is_error
    assert not seen


async def test_non_ai_entitlement_cannot_report_ai():
    seen = kaiten(lambda r: httpx.Response(200, json={**ENT, "type": "NUMBER"}))
    result = await call(
        "kaiten_report_ai_usage",
        instance="acme-prod",
        entitlement="ai-credits",
        transaction_id="call-001",
        phase="reserve",
        model="assistant",
        request_type="COMPLETION",
        usage={},
    )
    assert result.is_error
    assert [r.method for r in seen] == ["GET"]


async def test_ai_denial_and_retry_conflict_codes_are_preserved():
    seen = kaiten(
        lambda r: (
            httpx.Response(200, json=ENT)
            if r.method == "GET"
            else httpx.Response(
                409,
                json={
                    "status": 409,
                    "code": "ReportEntitlementUsageMetric.TransactionIdReused",
                    "detail": "different phase body",
                },
            )
        )
    )
    result = await call(
        "kaiten_report_ai_usage",
        instance="acme-prod",
        entitlement="ai-credits",
        transaction_id="call-001",
        phase="reserve",
        model="assistant",
        request_type="COMPLETION",
        usage={},
    )
    assert result.is_error
    assert "TransactionIdReused" in result.content[0].text
    assert "Idempotency-Key" not in seen[-1].headers


async def test_history_and_reservations_preserve_paging_and_credit_strings():
    page = {
        "items": [{"transactionId": "call-001", "credits": {"charged": "0.000001"}}],
        "hasMore": True,
        "nextCursor": "next",
    }
    seen = kaiten(lambda r: httpx.Response(200, json=page))
    result = await call(
        "kaiten_get_ai_history",
        instance="acme-prod",
        entitlement="ai-credits",
        transaction_id="call:001",
        status="COMMITTED",
        cursor="a+b",
        limit=3,
    )
    assert result.structured_content == page
    assert dict(seen[-1].url.params) == {
        "transactionId": "call:001",
        "status": "COMMITTED",
        "cursor": "a+b",
        "limit": "3",
    }
    await call(
        "kaiten_get_ai_history",
        instance="acme-prod",
        entitlement="ai-credits",
        open_reservations=True,
    )
    assert seen[-1].url.path.endswith("/usage/reservations")
    assert not seen[-1].url.query


async def test_model_edit_merges_and_preserves_exact_costs_and_metadata():
    state = dict(MODEL)

    def handler(r):
        if r.method == "PUT":
            state.update(json.loads(r.content))
            return httpx.Response(204)
        return httpx.Response(200, json=state)

    seen = kaiten(handler)
    unchanged = await call("kaiten_save_ai_model", model="assistant", name="Assistant")
    assert unchanged.structured_content["status"] == "unchanged"
    result = await call("kaiten_save_ai_model", model="assistant", status="DEPRECATED")
    assert result.structured_content["status"] == "updated"
    body = json.loads(next(r.content for r in seen if r.method == "PUT"))
    assert body["vendorCosts"] == MODEL["vendorCosts"]
    assert body["metadata"] == MODEL["metadata"]


async def test_create_model_has_no_provider_side_effects():
    seen = kaiten(
        lambda r: (
            httpx.Response(404, json={"status": 404})
            if r.method == "GET"
            else httpx.Response(201, json=MODEL)
        )
    )
    result = await call(
        "kaiten_save_ai_model",
        model="assistant",
        name="Assistant",
        vendor="own",
        modality="TEXT",
        external_ids=["provider/assistant-2026"],
        vendor_costs={"perInputToken": "0.000000000001"},
    )
    assert result.structured_content["status"] == "created"
    assert [r.url.path for r in seen] == ["/api/models/assistant", "/api/models"]
    assert json.loads(seen[-1].content)["vendorCosts"]["perInputToken"] == "0.000000000001"


async def test_rule_edit_is_version_scoped_and_omissions_preserve_selectors_and_caps():
    def handler(r):
        if r.url.path == "/api/entitlements/ai-credits":
            return httpx.Response(200, json=ENT)
        if r.method == "PUT":
            return httpx.Response(200, json={**RULE, **json.loads(r.content)})
        return httpx.Response(200, json=RULE)

    seen = kaiten(handler)
    result = await call(
        "kaiten_save_ai_rule",
        license="premium-v1",
        entitlement="ai-credits",
        rule_id=RULE["id"],
        rates={"perInputToken": "0.0000000002"},
    )
    assert not result.is_error, result
    put = next(r for r in seen if r.method == "PUT")
    assert put.url.path == f"/api/licenses/premium-v1/conversion-rules/{RULE['id']}"
    body = json.loads(put.content)
    assert body["modelSlug"] == RULE["modelSlug"]
    assert body["maximumCreditsPerRequest"] == "1.000001"
    assert body["rates"] == {"perInputToken": "0.0000000002"}
    assert not any("license-families" in r.url.path for r in seen)


async def test_unchanged_rule_writes_nothing():
    seen = kaiten(
        lambda r: httpx.Response(
            200, json=ENT if r.url.path.startswith("/api/entitlements") else RULE
        )
    )
    result = await call(
        "kaiten_save_ai_rule",
        license="premium-v1",
        entitlement="ai-credits",
        rule_id=RULE["id"],
        rates=RULE["rates"],
    )
    assert result.structured_content["status"] == "unchanged"
    assert all(r.method == "GET" for r in seen)


@pytest.mark.parametrize(
    "patch",
    [
        {"rates": {"perInputToken": 0.1}},
        {"rates": {"perInputToken": "0.00000000001"}},
        {"rates": {"bad": "1"}},
        {"minimum_credits": "2", "maximum_credits": "1"},
    ],
)
async def test_invalid_rule_never_writes(patch):
    seen = kaiten(lambda r: httpx.Response(200, json=ENT))
    result = await call(
        "kaiten_save_ai_rule",
        **{
            **dict(license="premium-v1", entitlement="ai-credits", rates={"perRequest": "1"}),
            **patch,
        },
    )
    assert result.is_error
    assert not seen


async def test_ai_requires_sum_before_entitlement_write():
    seen = kaiten(lambda r: httpx.Response(200, json=ENT))
    result = await call("kaiten_save_entitlement", entitlement="ai-credits", aggregation="COUNT")
    assert result.is_error
    assert "SUM" in result.content[0].text
    assert [r.method for r in seen] == ["GET"]


async def test_ai_read_only_and_idempotent_phase_annotations():
    async with Client(build(writes=False, ai_enabled=True)) as client:
        names = {t.name for t in await client.list_tools()}
    assert {"kaiten_get_ai_models", "kaiten_get_ai_rules", "kaiten_get_ai_history"} <= names
    assert "kaiten_report_ai_usage" not in names
    async with Client(build(writes=True, ai_enabled=True)) as client:
        tools = {t.name: t for t in await client.list_tools()}
    assert tools["kaiten_report_ai_usage"].annotations.idempotent_hint
    assert not tools["kaiten_report_usage"].annotations.idempotent_hint


def test_decimal_and_usage_boundaries_preserve_strings():
    assert decimal_string("999999999999.999999", "credits", 12, 6) == "999999999999.999999"
    assert rates_in({"perRequest": "99999999.9999999999", "perUnit": None})["perUnit"] is None
    assert (
        usage_in({"inputTokens": 2**63 - 1, "units": "0.000001", "unitType": "image"})["units"]
        == "0.000001"
    )


async def test_commit_can_inherit_model_and_request_type():
    seen = kaiten(
        lambda r: httpx.Response(
            200, json=ENT if r.method == "GET" else {"ai": {"status": "RELEASED"}}
        )
    )
    result = await call(
        "kaiten_report_ai_usage",
        instance="acme-prod",
        entitlement="ai-credits",
        transaction_id="call-001",
        phase="commit",
        request_status="NOT_SENT",
        usage={},
    )
    assert not result.is_error
    value = json.loads(seen[-1].content)["value"]
    assert "model" not in value and "requestType" not in value


async def test_ai_numeric_set_is_refused_but_append_adjustments_remain(monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from kaiten_mcp import views

    inst = SimpleNamespace(slug="acme-prod", license_slug="premium-v1")
    grant = SimpleNamespace(
        entitlement_slug="ai-credits",
        entitlement_type="NUMBER_AI_CREDIT",
        value={"type": "number", "value": 100},
        limit_cap_exceeded_overage_percent=0,
    )
    snap = SimpleNamespace(grants={"premium-v1": [grant]}, entitlements={}, usage={})
    monkeypatch.setattr(views, "instance", AsyncMock(return_value=inst))
    monkeypatch.setattr(views, "snapshot", AsyncMock(return_value=snap))
    seen = kaiten(
        lambda r: httpx.Response(
            200,
            json={
                "entitlementSlug": "ai-credits",
                "entitlementId": "e1",
                "instanceId": "i1",
                "licenseId": "l1",
                "licenseSlug": "premium-v1",
                "value": {"type": "number", "value": 1},
                "currentPeriodStart": "2026-01-01T00:00:00Z",
                "currentPeriodEnd": "2026-02-01T00:00:00Z",
            },
        )
    )
    refused = await call(
        "kaiten_report_usage",
        instance="acme-prod",
        entitlement="ai-credits",
        amount=1,
        behavior="set",
    )
    assert refused.is_error and not seen
    result = await call(
        "kaiten_report_usage",
        instance="acme-prod",
        entitlement="ai-credits",
        amount=-1,
        behavior="append",
    )
    assert not result.is_error, result
    assert json.loads(seen[-1].content)["value"] == {"type": "number", "value": -1.0}


async def test_rule_creation_preserves_decimal_strings():
    seen = kaiten(
        lambda r: (
            httpx.Response(200, json=ENT) if r.method == "GET" else httpx.Response(201, json=RULE)
        )
    )
    result = await call(
        "kaiten_save_ai_rule",
        license="premium-v1",
        entitlement="ai-credits",
        model="assistant",
        request_type="COMPLETION",
        rates={"perInputToken": "0.0000000001"},
        minimum_credits="0.000001",
        maximum_credits="999999999999.999999",
    )
    assert not result.is_error
    body = json.loads(seen[-1].content)
    assert body["rates"]["perInputToken"] == "0.0000000001"
    assert body["maximumCreditsPerRequest"] == "999999999999.999999"
    assert seen[-1].url.path == "/api/licenses/premium-v1/conversion-rules"


async def test_normalized_ai_numeric_set_still_requires_append_when_enabled(monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from kaiten_mcp import views

    inst = SimpleNamespace(slug="acme-prod", license_slug="premium-v1")
    grant = SimpleNamespace(entitlement_slug="ai-credits", entitlement_type="NUMBER")
    snap = SimpleNamespace(
        grants={"premium-v1": [grant]},
        entitlements={"ai-credits": SimpleNamespace(type="NUMBER_AI_CREDIT")},
    )
    monkeypatch.setattr(views, "instance", AsyncMock(return_value=inst))
    monkeypatch.setattr(views, "snapshot", AsyncMock(return_value=snap))
    seen = kaiten(lambda r: httpx.Response(500))
    result = await call(
        "kaiten_report_usage",
        instance=inst.slug,
        entitlement="ai-credits",
        amount=1,
        behavior="set",
    )
    assert result.is_error and not seen
