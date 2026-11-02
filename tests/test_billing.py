# Copyright 2026 KAITEN INC
# SPDX-License-Identifier: Apache-2.0

"""The billing tools, driven through a real MCP client and the real SDK, over a mocked Kaiten.

The mocked answers are whole: the SDK reads each one into its model, generated from Kaiten's
OpenAPI document, and refuses one that lacks a member the contract requires.
"""

import json
from typing import get_args

import httpx
import pytest
from kaitencloud.types import BillingDisabledReason
from test_tools import call, fresh_client, kaiten  # noqa: F401  -- fresh_client is autouse

from kaiten_mcp.tools import access, billing

STAMPS = {"createdAt": "2026-10-01T00:00:00Z", "updatedAt": "2026-10-01T00:00:00Z"}
PERIOD = {"serviceFrom": "2026-10-30T10:00:00Z", "serviceTo": "2026-11-30T10:00:00Z"}
INSTANCE = {
    "id": "i1",
    "slug": "mcp-test-instance",
    "name": "mcp-test instance",
    "description": "",
    "customerId": "c1",
    "customerSlug": "mcp-test-customer",
    "licenseId": "l1",
    "licenseSlug": "mcp-test-plan",
    "startLicenseDate": "2026-10-01T00:00:00Z",
    "endLicenseDate": "2027-10-01T00:00:00Z",
    "createdBy": {"id": "u", "name": "u"},
    "updatedBy": {"id": "u", "name": "u"},
    **STAMPS,
}
LICENSE = {
    "id": "l1",
    "slug": "mcp-test-plan",
    "name": "mcp-test plan",
    "description": "",
    "type": "PAID",
    "version": "1",
    "isDefault": False,
    "lifecycleState": "PUBLISHED",
    **STAMPS,
}
MONTHLY_ID = "00000000-0000-4000-8000-000000000001"
ANNUAL_ID = "00000000-0000-4000-8000-000000000002"
INVOICE_ID = "00000000-0000-4000-8000-0000000000aa"
SETTINGS = {
    "defaultCollectionMethod": "SEND_INVOICE",
    "defaultDaysUntilDue": 30,
    "handoffStripeInvoices": False,
}
NOOP = {
    "kind": "NOOP",
    "available": True,
    "connected": True,
    "livemode": None,
    "unavailableReason": None,
    "capabilities": {
        "automaticCollection": False,
        "billingPortal": False,
        "paymentMethodCapture": False,
    },
}
ADDON = {
    "id": "a1",
    "slug": "mcp-test-addon",
    "familySlug": "mcp-test-addon",
    "name": "Pack",
    "description": "",
    "version": 1,
    "versionName": "Version - 1",
    "pricingType": "PAID",
    "lifecycleState": "PUBLISHED",
    "isDefault": True,
    "maxQuantity": 5,
    "increaseBilling": "TRUE_UP",
    **STAMPS,
}
REDEMPTION = {
    "id": "r1",
    "instanceSlug": "mcp-test-instance",
    "voucherId": "v1",
    "voucherName": "mcp-test welcome",
    "voucherType": "PRICE",
    "codeHint": "COME",
    "status": "ACTIVE",
    "redeemedAt": "2026-10-30T10:00:00Z",
    "effectiveStartsAt": "2026-10-30T10:00:00Z",
    "effectiveExpiresAt": None,
    "expiredAt": None,
    "revokedAt": None,
    "revokedReason": None,
    "applicationsCount": 1,
    "applicationsMax": None,
}


def capabilities(*features: str, **extra) -> dict:
    """GET /billing/capabilities where billing is on, with these features."""
    names = ("addons", "chargeAutomatically", "lifecycle", "publicSurface", "stripe", "trials")
    names += ("vouchers",)
    return {
        "enabled": True,
        "disabledReason": None,
        "providers": [],
        "features": {name: name in features for name in names},
        "publicSurface": {"enabled": True},
        "usageHistoryRetentionMonths": None,
        "usageHistoryRetentionSource": "ENV",
        "usageIdempotencyWindowDays": 30,
        **extra,
    }


def price(pid: str, period: str, amount: str, **extra) -> dict:
    return {
        "id": pid,
        "billingModel": "FLAT_FEE",
        "billingTiming": "ADVANCE",
        "billingPeriod": period,
        "currency": "EUR",
        "unitAmount": int(amount),
        "unitAmountDecimal": amount,
        "metered": None,
        "displayLabel": f"mcp-test, {period.lower()}",
        "displayOrder": 0,
        "isDefault": True,
        "status": "ACTIVE",
        "deprecatedAt": None,
        **STAMPS,
        **extra,
    }


def subscription(status: str = "ACTIVE", **extra) -> dict:
    return {
        "id": "s1",
        "status": status,
        "basePrice": price(MONTHLY_ID, "MONTHLY", "2900"),
        "billingPeriod": "MONTHLY",
        "currency": "EUR",
        "providerKind": "NOOP",
        "collectionMethod": "SEND_INVOICE",
        "collectionMethodOverride": None,
        "daysUntilDue": 30,
        "daysUntilDueOverride": None,
        "anchorAt": "2026-10-30T10:00:00Z",
        "startedAt": "2026-10-30T10:00:00Z",
        "currentPeriodStart": "2026-10-30T10:00:00Z",
        "currentPeriodEnd": "2026-11-30T10:00:00Z",
        "cancelAtPeriodEnd": False,
        "customerSlug": "mcp-test-customer",
        "customerName": "mcp-test customer",
        "instanceSlug": "mcp-test-instance",
        "instanceName": "mcp-test instance",
        **dict.fromkeys(  # what a subscription that runs leaves null
            (
                "cancelRequestedAt",
                "canceledAt",
                "cancellationReason",
                "incompleteExpiresAt",
                "pastDueSince",
                "pendingLicenseSlug",
                "reminderAt",
                "scheduledChange",
                "trialEndsAt",
            )
        ),
        **STAMPS,
        **extra,
    }


def line(amount: int = 2900) -> dict:
    """The BASE line of an invoice: the flat fee."""
    return {
        "type": "BASE",
        "label": "mcp-test, monthly",
        "description": "1 x 29.00 EUR",
        "amount": amount,
        "quantity": "1",
        "unitAmountDecimal": str(amount),
        "seq": 1,
        "billingModel": "FLAT_FEE",
        "billingTiming": "ADVANCE",
        "licensePriceId": MONTHLY_ID,
        **dict.fromkeys(  # what only another kind of line carries
            (
                "addonId",
                "addonPriceId",
                "discount",
                "entitlementId",
                "entitlementSlug",
                "instanceAddonId",
                "instanceVoucherId",
                "metering",
                "overage",
                "provider",
                "trueUp",
                "voucherId",
            )
        ),
        **PERIOD,
    }


def summary(status: str = "MANUAL", **extra) -> dict:
    """An invoice as a list, or the answer of a write, carries it: without its lines."""
    return {
        "id": INVOICE_ID,
        "kind": "ACTIVATION",
        "status": status,
        "boundaryAt": "2026-10-30T10:00:00Z",
        "holdReason": None,
        "providerKind": "NOOP",
        "collectionMethod": "SEND_INVOICE",
        "currency": "EUR",
        "subtotal": 2900,
        "discountTotal": 0,
        "total": 2900,
        "issuedAt": "2026-10-30T10:00:00Z",
        "daysUntilDue": 30,
        "dueAt": "2026-11-29T10:00:00Z",
        "paidAt": None,
        "customerSlug": "mcp-test-customer",
        "customerName": "mcp-test customer",
        "instanceSlug": "mcp-test-instance",
        "instanceName": "mcp-test instance",
        "licenseSlug": "mcp-test-plan",
        "handoffStatus": "PENDING",
        "overdue": False,
        **PERIOD,
        **STAMPS,
        **extra,
    }


def invoice(status: str = "MANUAL", reference: str | None = None, **extra) -> dict:
    """An invoice as GET /invoices/{id} answers it: with its lines and its handoff."""
    handoff = "ACKNOWLEDGED" if reference else "PENDING"
    return {
        **summary(status, handoffStatus=handoff),
        "lines": [line()],
        "handoff": {
            "status": handoff,
            "externalReference": reference,
            "acknowledgedAt": None,
            "claimCount": 0,
            "leaseId": None,
            "leasedUntil": None,
        },
        "licenseId": "l1",
        **dict.fromkeys(  # what a MANUAL invoice leaves null
            (
                "billingEmail",
                "hold",
                "holdDetail",
                "provider",
                "replacedByInvoiceId",
                "replacesInvoiceId",
                "uncollectibleAt",
                "voidReason",
                "voidedAt",
            )
        ),
        **extra,
    }


def preview(total: int) -> dict:
    """An invoice composed and not issued: a preview, or the upcoming invoice."""
    return {
        "status": "PREVIEW",
        "kind": "RENEWAL",
        "asOf": "2026-11-02T00:00:00Z",
        "boundaryAt": "2026-11-30T10:00:00Z",
        "licenseSlug": "mcp-test-plan",
        "currency": "EUR",
        "subtotal": total,
        "discountTotal": 0,
        "total": total,
        "lines": [line(total)],
        "wouldHold": [],
    }


def held(slug: str, quantity: int) -> dict:
    """An add-on an instance holds."""
    return {
        "id": "ia1",
        "addonId": "a1",
        "addonSlug": slug,
        "familySlug": slug,
        "name": "Pack",
        "quantity": quantity,
        "pendingQuantity": None,
        "maxQuantity": 5,
        "prices": [],
        "attachedAt": "2026-10-30T10:00:00Z",
        "removedAt": None,
    }


def voucher(**extra) -> dict:
    return {
        "id": "v1",
        "code": "MCP-TEST-WELCOME",
        "codeHint": "COME",
        "name": "mcp-test welcome",
        "description": None,
        "voucherType": "PRICE",
        "status": "ACTIVE",
        "duration": "ONE_TIME",
        "durationInPeriods": None,
        "priceDiscountType": "PERCENTAGE",
        "priceDiscountValue": "10",
        "currency": None,
        "priceAppliesTo": "LICENSE_BASE",
        "grants": [],
        "flagGrants": [],
        "applicableLicenseIds": [],
        "applicableLicensePriceIds": [],
        "applicableAddonIds": [],
        "applicableAddonPriceIds": [],
        "redemptionRules": {},
        "redemptionsCount": 0,
        "maxRedemptions": None,
        "restrictedCustomerSlug": None,
        "startsAt": None,
        "expiresAt": None,
        **STAMPS,
        **extra,
    }


def problem(status: int, code: str, detail: str = "refused") -> tuple[int, dict]:
    return status, {"status": status, "code": code, "detail": detail}


def api(routes: dict):
    """A Kaiten answering (method, path) with JSON, (status, JSON), or a function of the request.
    Anything else is a 404. Returns every request it got."""

    def handler(r: httpx.Request) -> httpx.Response:
        answer = routes.get((r.method, r.url.path.removeprefix("/api")))  # the SDK's /api prefix
        if callable(answer):
            answer = answer(r)
        if answer is None:
            answer = problem(404, "Test.NotFound", f"{r.method} {r.url.path}")
        status, body = answer if isinstance(answer, tuple) else (200, answer)
        return httpx.Response(status, json=body)

    return kaiten(handler)


def sent(seen: list[httpx.Request], method: str, path: str) -> list[dict]:
    """The bodies of the requests that reached path."""
    return [
        json.loads(r.content or b"null")
        for r in seen
        if r.method == method and r.url.path == f"/api/{path}"
    ]


@pytest.mark.parametrize(
    ("reason", "code", "why"),
    [
        (
            "DEPLOYMENT_DISABLED",
            "Billing.Disabled",
            "operator turns it on with KAITEN_BILLING_ENABLED",
        ),
        ("NOT_ENTITLED", "Billing.NotEntitled", "not included in this organization's Kaiten plan"),
    ],
)
async def test_billing_off_is_explained_by_the_reason_kaiten_gives(reason, code, why):
    api(
        {
            ("GET", "/billing/capabilities"): capabilities(enabled=False, disabledReason=reason),
            ("GET", "/invoices"): problem(403, code, "billing is off"),
        }
    )
    off = (await call("kaiten_get_billing")).structured_content["billing"]
    assert (off["enabled"], off["reason"]) == (False, reason)
    assert why in off["explanation"] and "work without it" in off["explanation"]

    # Any other billing tool Kaiten refuses for it says the same, and names the refusal.
    refused = await call("kaiten_get_invoices")
    assert refused.is_error
    assert refused.content[0].text == f"{off['explanation']} ({code})"


def test_every_reason_billing_can_be_off_for_is_explained():
    # The reasons are the SDK's, generated from the OpenAPI: one added there fails here first.
    assert set(billing.BILLING_OFF) == set(get_args(BillingDisabledReason))


@pytest.mark.parametrize(
    "capability",
    [capabilities(), problem(403, "Auth.MissingScope", "requires read:billing")],
    ids=["billing on", "capabilities unreadable"],
)
async def test_any_other_refusal_reaches_the_agent_as_kaiten_gave_it(capability):
    api(
        {
            ("GET", "/billing/capabilities"): capability,
            ("GET", "/invoices"): problem(403, "Auth.MissingScope", "requires read:billing"),
        }
    )
    refused = await call("kaiten_get_invoices")
    assert refused.content[0].text == "Kaiten API 403 Auth.MissingScope: requires read:billing"


async def test_an_entitlement_check_kaiten_could_not_make_is_its_own_answer():
    # Kaiten's own words, which say what the agent can do: no explanation of the server's is needed.
    detail = (
        "whether this organization's plan includes billing could not be checked; "
        "nothing was changed, retry"
    )
    seen = api({("GET", "/invoices"): problem(503, "Billing.EntitlementCheckUnavailable", detail)})
    refused = await call("kaiten_get_invoices")
    assert (
        refused.content[0].text == f"Kaiten API 503 Billing.EntitlementCheckUnavailable: {detail}"
    )
    assert [r.url.path for r in seen] == ["/api/invoices"]  # no reason to ask whether billing is on


async def test_the_overview_shows_what_the_token_reaches():
    api(
        {
            ("GET", "/billing/capabilities"): capabilities("addons", "vouchers", providers=[NOOP]),
            ("GET", "/billing/settings"): SETTINGS,
            ("GET", "/addon-families"): [
                {
                    "id": "af1",
                    "slug": "mcp-test-addon",
                    "isPublic": False,
                    "lastVersion": 1,
                    "increaseBilling": "TRUE_UP",
                    "currentVersion": ADDON,
                    "versions": [ADDON],
                }
            ],
            ("GET", "/vouchers"): problem(403, "Auth.MissingScope", "requires read:vouchers"),
        }
    )
    out = (await call("kaiten_get_billing")).structured_content
    assert out["billing"] == {
        "enabled": True,
        "providers": [{"kind": "NOOP", "connected": True}],
        "features": ["addons", "vouchers"],
        "default_collection_method": "SEND_INVOICE",
        "default_days_until_due": 30,
    }
    assert out["addons"] == [
        {
            "family": "mcp-test-addon",
            "current_version": "mcp-test-addon",
            "name": "Pack",
            "pricing": "PAID",
            "max_quantity": 5,
            "versions": {"mcp-test-addon": "PUBLISHED"},
        }
    ]
    assert "read:vouchers" in out["vouchers"]["unavailable"]


async def test_a_voucher_list_never_shows_a_code():
    api(
        {
            ("GET", "/billing/capabilities"): capabilities(),
            ("GET", "/billing/settings"): SETTINGS,
            ("GET", "/addon-families"): [],
            ("GET", "/vouchers"): {"items": [voucher()], "hasMore": False},
        }
    )
    out = (await call("kaiten_get_billing")).structured_content
    assert out["vouchers"][0]["code_hint"] == "COME"
    assert "MCP-TEST-WELCOME" not in json.dumps(out)


async def test_an_instance_reads_as_its_subscription_next_invoice_addons_and_invoices():
    base = "/instances/mcp-test-instance"
    seen = api(
        {
            ("GET", base): INSTANCE,
            ("GET", f"{base}/billing"): subscription(),
            ("GET", f"{base}/billing/upcoming-invoice"): preview(3200),
            ("GET", f"{base}/addons"): [held("mcp-test-addon", 2)],
            ("GET", f"{base}/vouchers"): [REDEMPTION],
            ("GET", f"{base}/invoices"): {"items": [summary()], "hasMore": False},
        }
    )
    out = (await call("kaiten_get_billing", instance="mcp-test-instance")).structured_content
    assert out["subscription"]["status"] == "ACTIVE"
    assert out["subscription"]["next_boundary"] == "2026-11-30T10:00:00Z"
    assert out["upcoming_invoice"]["total"] == 3200
    assert out["upcoming_invoice"]["lines"][0]["amount"] == 3200
    assert out["addons"] == [
        {
            "addon": "mcp-test-addon",
            "family": "mcp-test-addon",
            "name": "Pack",
            "quantity": 2,
            "max_quantity": 5,
        }
    ]
    assert out["vouchers"][0]["code_hint"] == "COME"
    assert out["vouchers"][0]["invoices_discounted"] == 1
    [latest] = out["latest_invoices"]
    assert latest["id"] == INVOICE_ID and "lines" not in latest  # a list's invoice is a summary
    [listed] = [r for r in seen if r.url.path == f"/api{base}/invoices"]
    assert listed.url.params["limit"] == "5"


async def test_prices_come_with_a_preview_from_a_sample():
    seen = api(
        {
            ("GET", "/licenses/mcp-test-plan"): LICENSE,
            ("GET", "/licenses/mcp-test-plan/prices"): [price(MONTHLY_ID, "MONTHLY", "2900")],
            ("POST", "/licenses/mcp-test-plan/invoice-preview"): preview(3200),
        }
    )
    out = (
        await call(
            "kaiten_get_prices", license="mcp-test-plan", sample_usage={"monthly-orders": 106}
        )
    ).structured_content
    assert out["prices"][0]["unit_amount"] == "2900" and out["preview"]["total"] == 3200
    assert sent(seen, "POST", "licenses/mcp-test-plan/invoice-preview") == [
        {"sampleUsage": [{"entitlementSlug": "monthly-orders", "quantity": "106"}]}
    ]


async def test_save_prices_deprecates_first_and_adds_only_what_is_new():
    prices = [price(MONTHLY_ID, "MONTHLY", "2900")]

    def deprecate(r):
        prices[0] = {**prices[0], "status": "DEPRECATED", "isDefault": False}
        return prices[0]

    def create(r):
        body = json.loads(r.content)
        prices.append(price(ANNUAL_ID, body["billingPeriod"], body["unitAmountDecimal"]))
        return 201, prices[-1]

    seen = api(
        {
            ("GET", "/licenses/mcp-test-plan"): LICENSE,
            ("GET", "/licenses/mcp-test-plan/prices"): lambda r: [
                p for p in prices if r.url.params.get("status") in (None, p["status"])
            ],
            ("POST", f"/licenses/mcp-test-plan/prices/{MONTHLY_ID}/deprecate"): deprecate,
            ("POST", "/licenses/mcp-test-plan/prices"): create,
        }
    )
    monthly = {"billing_model": "FLAT_FEE", "unit_amount": 2900, "currency": "EUR"}
    monthly |= {"billing_period": "MONTHLY", "is_default": True}
    same = await call("kaiten_save_prices", license="mcp-test-plan", add=[monthly])
    assert same.structured_content["status"] == "unchanged"
    assert sent(seen, "POST", "licenses/mcp-test-plan/prices") == []

    # A new monthly default replaces the old one: the old one goes first.
    out = (
        await call(
            "kaiten_save_prices",
            license="mcp-test-plan",
            add=[{**monthly, "unit_amount": "3400"}],
            deprecate=[MONTHLY_ID],
        )
    ).structured_content
    assert out["status"] == "updated"
    assert [p["unit_amount"] for p in out["changes"]["added"]] == ["3400"]
    assert [p["status"] for p in out["changes"]["deprecated"]] == ["DEPRECATED"]
    assert sent(seen, "POST", "licenses/mcp-test-plan/prices")[-1] == {
        "billingModel": "FLAT_FEE",
        "currency": "EUR",
        "unitAmountDecimal": "3400",
        "billingPeriod": "MONTHLY",
        "isDefault": True,
    }
    assert [p["unit_amount"] for p in out["pricing"]["prices"]] == ["3400"]


async def test_a_refused_price_is_reported_with_what_was_written_before_it():
    api(
        {
            ("GET", "/licenses/mcp-test-plan"): LICENSE,
            ("GET", "/licenses/mcp-test-plan/prices"): [price(MONTHLY_ID, "MONTHLY", "2900")],
            ("POST", f"/licenses/mcp-test-plan/prices/{MONTHLY_ID}/deprecate"): price(
                MONTHLY_ID, "MONTHLY", "2900", status="DEPRECATED"
            ),
            ("POST", "/licenses/mcp-test-plan/prices"): problem(
                409, "CreateLicensePrice.BillingActive", "a subscription bills this version"
            ),
        }
    )
    out = (
        await call(
            "kaiten_save_prices",
            license="mcp-test-plan",
            add=[
                {
                    "billing_model": "FLAT_FEE",
                    "unit_amount": 3400,
                    "currency": "EUR",
                    "billing_period": "MONTHLY",
                }
            ],
            deprecate=[MONTHLY_ID],
        )
    ).structured_content
    assert out["status"] == "updated" and "deprecated" in out["changes"]
    assert "CreateLicensePrice.BillingActive" in out["reason"]


async def test_subscribe_bills_the_default_of_a_period_and_reports_its_first_invoice():
    state: dict = {}

    def subscribe(r):
        state["now"] = subscription()
        return 201, {**state["now"], "activationInvoice": summary()}

    seen = api(
        {
            ("GET", "/instances/mcp-test-instance"): INSTANCE,
            ("GET", "/instances/mcp-test-instance/billing"): lambda r: (
                state.get("now") or problem(404, "GetInstanceBilling.NotFound")
            ),
            ("GET", "/licenses/mcp-test-plan/prices"): [
                price(MONTHLY_ID, "MONTHLY", "2900"),
                price(ANNUAL_ID, "ANNUAL", "29000"),
            ],
            ("POST", "/instances/mcp-test-instance/billing"): subscribe,
        }
    )
    out = (
        await call(
            "kaiten_subscribe_instance", instance="mcp-test-instance", billing_period="MONTHLY"
        )
    ).structured_content
    assert out["status"] == "created" and out["subscription"]["status"] == "ACTIVE"
    assert out["activation_invoice"]["total"] == 2900
    assert sent(seen, "POST", "instances/mcp-test-instance/billing") == [
        {"basePriceId": MONTHLY_ID, "providerKind": "NOOP"}
    ]
    fees = next(r for r in seen if r.url.path == "/api/licenses/mcp-test-plan/prices")
    assert dict(fees.url.params) == {"status": "ACTIVE", "billingModel": "FLAT_FEE"}

    again = await call(
        "kaiten_subscribe_instance", instance="mcp-test-instance", billing_period="MONTHLY"
    )
    assert again.structured_content["status"] == "unchanged"
    ambiguous = await call("kaiten_subscribe_instance", instance="mcp-test-instance")
    assert ambiguous.is_error and "Pass billing_period or price" in ambiguous.content[0].text


async def test_a_cancellation_at_period_end_is_asked_once():
    state = {"now": subscription()}

    def cancel(r):
        state["now"] = subscription(cancelAtPeriodEnd=True)
        return state["now"]

    seen = api(
        {
            ("GET", "/instances/mcp-test-instance"): INSTANCE,
            ("GET", "/instances/mcp-test-instance/billing"): lambda r: state["now"],
            ("POST", "/instances/mcp-test-instance/billing/cancel"): cancel,
        }
    )
    out = (
        await call("kaiten_cancel_subscription", instance="mcp-test-instance", reason="mcp-test")
    ).structured_content
    assert out["status"] == "updated"
    assert out["changes"] == {"cancel_at_period_end": {"from": None, "to": True}}
    assert sent(seen, "POST", "instances/mcp-test-instance/billing/cancel") == [
        {"mode": "AT_PERIOD_END", "reason": "mcp-test"}
    ]
    again = await call("kaiten_cancel_subscription", instance="mcp-test-instance")
    assert again.structured_content["status"] == "unchanged"


async def test_an_addon_is_attached_changed_and_detached():
    holding: list[dict] = []
    path = "/instances/mcp-test-instance/addons"

    def attach(r):
        body = json.loads(r.content)
        holding.append(held(body["addonSlug"], body["quantity"]))
        return 201, holding[-1]

    def change(r):
        holding[0] = {**holding[0], "quantity": json.loads(r.content)["quantity"]}
        return holding[0]

    api(
        {
            ("GET", path): lambda r: holding,
            ("POST", path): attach,
            ("PATCH", f"{path}/mcp-test-addon"): change,
            ("DELETE", f"{path}/mcp-test-addon"): lambda r: (holding.clear(), (204, None))[1],
        }
    )

    async def set_to(quantity: int) -> dict:
        args = {"instance": "mcp-test-instance", "addon": "mcp-test-addon", "quantity": quantity}
        return (await call("kaiten_set_instance_addon", **args)).structured_content

    assert (await set_to(2))["status"] == "created"
    assert (await set_to(2))["status"] == "unchanged"
    assert (await set_to(3))["changes"] == {"quantity": {"from": 2, "to": 3}}
    assert (await set_to(0))["status"] == "deleted"
    assert (await set_to(0))["status"] == "unchanged"


async def test_a_voucher_takes_its_kind_from_what_it_does_and_is_published():
    vouchers: dict = {}

    def create(r):
        vouchers["v1"] = voucher(status="DRAFT") | json.loads(r.content)
        return 201, vouchers["v1"]

    def publish(r):
        vouchers["v1"] |= {"status": "ACTIVE"}
        return vouchers["v1"]

    seen = api(
        {
            ("POST", "/vouchers/lookup"): lambda r: (
                vouchers.get("v1") or problem(404, "LookupVoucher.NotFound")
            ),
            ("POST", "/vouchers"): create,
            ("POST", "/vouchers/v1/publish"): publish,
        }
    )
    out = (
        await call(
            "kaiten_save_voucher",
            code="MCP-TEST-WELCOME",
            name="mcp-test welcome",
            discount={"percent": 10},
            boosts={"monthly-orders": {"modifier": "ADD", "value": 50}},
            duration="REPEATING",
            periods=3,
        )
    ).structured_content
    assert out["status"] == "created" and out["voucher"]["status"] == "ACTIVE"
    assert (out["voucher"]["code"], out["voucher"]["code_hint"]) == ("MCP-TEST-WELCOME", "COME")
    assert out["voucher"]["boosts"] == {"monthly-orders": {"modifier": "ADD", "value": "50"}}
    again = (await call("kaiten_save_voucher", code="MCP-TEST-WELCOME")).structured_content
    assert again["status"] == "unchanged" and again["voucher"]["code_hint"] == "COME"
    assert "MCP-TEST-WELCOME" not in json.dumps(again)
    [body] = sent(seen, "POST", "vouchers")
    assert body["voucherType"] == "COMPOSITE"
    assert (body["priceDiscountType"], body["priceDiscountValue"]) == ("PERCENTAGE", "10")
    assert body["grants"] == [
        {"entitlementSlug": "monthly-orders", "modifierType": "ADD", "modifierValue": "50"}
    ]


async def test_a_redemption_refused_or_repeated_writes_nothing():
    answers = iter(
        [
            problem(422, "RedeemVoucher.NotEligible", "not on an eligible license"),
            problem(409, "RedeemVoucher.AlreadyRedeemed", "already redeemed"),
            (201, REDEMPTION),
        ]
    )
    api({("POST", "/instances/mcp-test-instance/vouchers/redeem"): lambda r: next(answers)})
    args = {"instance": "mcp-test-instance", "code": "MCP-TEST-WELCOME"}
    refused = (await call("kaiten_redeem_voucher", **args)).structured_content
    assert refused["status"] == "rejected" and "NotEligible" in refused["reason"]
    assert (await call("kaiten_redeem_voucher", **args)).structured_content["status"] == "unchanged"
    redeemed = (await call("kaiten_redeem_voucher", **args)).structured_content
    assert redeemed["status"] == "created" and redeemed["redemption"]["code_hint"] == "COME"


async def test_an_invoice_collected_by_hand_is_marked_paid_once():
    state = {"invoice": invoice()}

    def mark_paid(r):
        reference = json.loads(r.content)["externalReference"]
        state["invoice"] = invoice("PAID", reference, paidAt="2026-10-30T12:00:00Z")
        return state["invoice"]

    seen = api(
        {
            ("GET", f"/invoices/{INVOICE_ID}"): lambda r: state["invoice"],
            ("POST", f"/invoices/{INVOICE_ID}/mark-paid"): mark_paid,
        }
    )
    args = {"invoice": INVOICE_ID, "external_reference": "mcp-test-0001"}
    out = (await call("kaiten_mark_invoice_paid", **args)).structured_content
    assert out["status"] == "updated"
    assert out["changes"]["status"] == {"from": "MANUAL", "to": "PAID"}
    assert out["invoice"]["external_reference"] == "mcp-test-0001"
    assert out["invoice"]["lines"][0]["type"] == "BASE"
    assert (await call("kaiten_mark_invoice_paid", **args)).structured_content[
        "status"
    ] == "unchanged"
    assert len(sent(seen, "POST", f"invoices/{INVOICE_ID}/mark-paid")) == 1


async def test_the_invoice_views_are_the_consoles():
    rows = [summary(id=f"00000000-0000-4000-8000-00000000000{n}") for n in range(3)]

    def page(r):
        limit = int(r.url.params["limit"])
        return {"items": rows[:limit], "hasMore": len(rows) > limit}

    seen = api({("GET", "/invoices"): page})
    out = (await call("kaiten_get_invoices", view="handoff", limit=1)).structured_content
    assert out["more"] is True and [i["id"] for i in out["invoices"]] == [rows[0]["id"]]
    assert out["invoices"][0]["handoff"] == "PENDING"
    out = (
        await call("kaiten_get_invoices", view="overdue", customer="mcp-test-customer")
    ).structured_content
    assert len(out["invoices"]) == 3 and "more" not in out
    # One invoice past the page tells whether there are more.
    params = [dict(r.url.params) for r in seen]
    assert params[0] == {"handoffStatus": "PENDING", "limit": "2"}
    assert params[1] == {"overdue": "true", "customerSlug": "mcp-test-customer", "limit": "26"}


def test_billing_scopes_can_be_issued():
    assert {"read:billing", "write:addons", "write:voucher_redemptions"} <= set(access.SCOPES)
