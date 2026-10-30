# Copyright 2026 KAITEN INC
# SPDX-License-Identifier: Apache-2.0

"""The billing tools, driven through a real MCP client and the real SDK, over a mocked Kaiten."""

import json

import httpx
from test_tools import call, fresh_client, kaiten  # noqa: F401  -- fresh_client is autouse

from kaiten_mcp.tools import access

STAMPS = {"createdAt": "2026-10-01T00:00:00Z", "updatedAt": "2026-10-01T00:00:00Z"}
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
        "daysUntilDue": 30,
        "startedAt": "2026-10-30T10:00:00Z",
        "currentPeriodStart": "2026-10-30T10:00:00Z",
        "currentPeriodEnd": "2026-11-30T10:00:00Z",
        "cancelAtPeriodEnd": False,
        "canceledAt": None,
        "scheduledChange": None,
        **extra,
    }


def invoice(status: str = "MANUAL", **extra) -> dict:
    return {
        "id": INVOICE_ID,
        "kind": "ACTIVATION",
        "status": status,
        "instanceSlug": "mcp-test-instance",
        "customerSlug": "mcp-test-customer",
        "licenseSlug": "mcp-test-plan",
        "providerKind": "NOOP",
        "boundaryAt": "2026-10-30T10:00:00Z",
        "currency": "EUR",
        "subtotal": 2900,
        "discountTotal": 0,
        "total": 2900,
        "overdue": False,
        "holdReason": None,
        "handoffStatus": "PENDING",
        "handoff": {"status": "PENDING", "externalReference": None},
        "lines": [
            {
                "type": "BASE",
                "label": "mcp-test, monthly",
                "description": "1 x 29.00 EUR",
                "amount": 2900,
                "serviceFrom": "2026-10-30T10:00:00Z",
                "serviceTo": "2026-11-30T10:00:00Z",
            }
        ],
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


async def test_billing_off_is_explained_not_raised():
    api(
        {
            ("GET", "/billing/capabilities"): {
                "enabled": False,
                "disabledReason": "NOT_ENTITLED",
                "providers": [],
                "features": {},
            },
            ("GET", "/invoices"): problem(403, "Billing.Disabled", "billing is disabled"),
        }
    )
    billing = (await call("kaiten_get_billing")).structured_content["billing"]
    assert billing["enabled"] is False and billing["reason"] == "NOT_ENTITLED"
    assert "not included in this organization's Kaiten plan" in billing["explanation"]

    refused = await call("kaiten_get_invoices")
    assert refused.is_error
    text = refused.content[0].text
    assert "KAITEN_BILLING_ENABLED" in text and "(Billing.Disabled)" in text
    assert "Traceback" not in text


async def test_the_overview_shows_what_the_token_reaches():
    api(
        {
            ("GET", "/billing/capabilities"): {
                "enabled": True,
                "providers": [{"kind": "NOOP", "connected": True, "livemode": None}],
                "features": {"addons": True, "vouchers": True, "stripe": False},
            },
            ("GET", "/billing/settings"): {
                "defaultCollectionMethod": "SEND_INVOICE",
                "defaultDaysUntilDue": 30,
            },
            ("GET", "/addon-families"): [
                {
                    "slug": "mcp-test-addon",
                    "currentVersion": {"slug": "mcp-test-addon", "name": "Pack", "maxQuantity": 5},
                    "versions": [{"slug": "mcp-test-addon", "lifecycleState": "PUBLISHED"}],
                }
            ],
            ("GET", "/vouchers"): problem(403, "Auth.MissingScope", "requires read:vouchers"),
        }
    )
    out = (await call("kaiten_get_billing")).structured_content
    assert out["billing"]["features"] == ["addons", "vouchers"]
    assert out["billing"]["default_days_until_due"] == 30
    assert out["addons"][0]["current_version"] == "mcp-test-addon"
    assert "read:vouchers" in out["vouchers"]["unavailable"]


async def test_a_voucher_list_never_shows_a_code():
    api(
        {
            ("GET", "/billing/capabilities"): {"enabled": True, "providers": [], "features": {}},
            ("GET", "/billing/settings"): {},
            ("GET", "/addon-families"): [],
            ("GET", "/vouchers"): {
                "items": [
                    {
                        "id": "v1",
                        "code": "MCP-TEST-WELCOME",
                        "codeHint": "COME",
                        "name": "mcp-test welcome",
                        "voucherType": "PRICE",
                        "status": "ACTIVE",
                    }
                ],
                "hasMore": False,
            },
        }
    )
    out = (await call("kaiten_get_billing")).structured_content
    assert out["vouchers"][0]["code_hint"] == "COME"
    assert "MCP-TEST-WELCOME" not in json.dumps(out)


async def test_prices_come_with_a_preview_from_a_sample():
    seen = api(
        {
            ("GET", "/licenses/mcp-test-plan"): LICENSE,
            ("GET", "/licenses/mcp-test-plan/prices"): [price(MONTHLY_ID, "MONTHLY", "2900")],
            ("POST", "/licenses/mcp-test-plan/invoice-preview"): {
                **invoice(status="PREVIEW", kind="RENEWAL"),
                "total": 3200,
            },
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
    assert sent(seen, "POST", "licenses/mcp-test-plan/prices")[-1]["unitAmountDecimal"] == "3400"
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
    billing: dict = {}

    def subscribe(r):
        billing["now"] = {**subscription(), "activationInvoice": invoice()}
        return 201, billing["now"]

    seen = api(
        {
            ("GET", "/instances/mcp-test-instance"): INSTANCE,
            ("GET", "/instances/mcp-test-instance/billing"): lambda r: (
                billing.get("now") or problem(404, "GetInstanceBilling.NotFound")
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

    again = await call(
        "kaiten_subscribe_instance", instance="mcp-test-instance", billing_period="MONTHLY"
    )
    assert again.structured_content["status"] == "unchanged"
    ambiguous = await call("kaiten_subscribe_instance", instance="mcp-test-instance")
    assert ambiguous.is_error and "Pass billing_period or price" in ambiguous.content[0].text


async def test_a_cancellation_at_period_end_is_asked_once():
    billing = {"now": subscription()}

    def cancel(r):
        billing["now"] = subscription(cancelAtPeriodEnd=True)
        return billing["now"]

    seen = api(
        {
            ("GET", "/instances/mcp-test-instance"): INSTANCE,
            ("GET", "/instances/mcp-test-instance/billing"): lambda r: billing["now"],
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
    held: list[dict] = []
    path = "/instances/mcp-test-instance/addons"

    def attach(r):
        body = json.loads(r.content)
        held.append(
            {
                "addonSlug": body["addonSlug"],
                "familySlug": body["addonSlug"],
                "name": "Pack",
                "quantity": body["quantity"],
                "pendingQuantity": None,
            }
        )
        return 201, held[-1]

    def change(r):
        held[0] = {**held[0], "quantity": json.loads(r.content)["quantity"]}
        return held[0]

    api(
        {
            ("GET", path): lambda r: held,
            ("POST", path): attach,
            ("PATCH", f"{path}/mcp-test-addon"): change,
            ("DELETE", f"{path}/mcp-test-addon"): lambda r: (held.clear(), (204, None))[1],
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
        vouchers["v1"] = {"id": "v1", "status": "DRAFT", "codeHint": "COME"}
        vouchers["v1"] |= json.loads(r.content)
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
        ]
    )
    api({("POST", "/instances/mcp-test-instance/vouchers/redeem"): lambda r: next(answers)})
    args = {"instance": "mcp-test-instance", "code": "MCP-TEST-WELCOME"}
    refused = (await call("kaiten_redeem_voucher", **args)).structured_content
    assert refused["status"] == "rejected" and "NotEligible" in refused["reason"]
    assert (await call("kaiten_redeem_voucher", **args)).structured_content["status"] == "unchanged"


async def test_an_invoice_collected_by_hand_is_marked_paid_once():
    state = {"invoice": invoice()}

    def mark_paid(r):
        reference = json.loads(r.content)["externalReference"]
        state["invoice"] = invoice(
            status="PAID",
            paidAt="2026-10-30T12:00:00Z",
            handoffStatus="ACKNOWLEDGED",
            handoff={"status": "ACKNOWLEDGED", "externalReference": reference},
        )
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
    assert (await call("kaiten_mark_invoice_paid", **args)).structured_content[
        "status"
    ] == "unchanged"
    assert len(sent(seen, "POST", f"invoices/{INVOICE_ID}/mark-paid")) == 1


async def test_the_invoice_views_are_the_consoles():
    seen = api({("GET", "/invoices"): {"items": [invoice()], "hasMore": True}})
    out = (await call("kaiten_get_invoices", view="handoff", limit=1)).structured_content
    assert out["more"] is True and out["invoices"][0]["handoff"] == "PENDING"
    await call("kaiten_get_invoices", view="overdue", customer="mcp-test-customer")
    params = [dict(r.url.params) for r in seen]
    assert params[0] == {"handoffStatus": "PENDING", "limit": "1"}
    assert params[1] == {"overdue": "true", "customerSlug": "mcp-test-customer", "limit": "25"}


def test_billing_scopes_can_be_issued():
    assert {"read:billing", "write:addons", "write:voucher_redemptions"} <= set(access.SCOPES)
