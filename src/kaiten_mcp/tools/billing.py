# Copyright 2026 KAITEN INC
# SPDX-License-Identifier: Apache-2.0

"""Billing: what a License version costs, the subscription that bills an instance, its invoices,
and the add-ons and vouchers on top of its License.

Kaiten composes every invoice, from the prices and the usage journal; Stripe, or the
organization's own accounting system through the handoff queue, collects it. Billing is off until
it is switched on, and its routes refuse every call until then: kaiten_get_billing says why, and
sdk.call_unwrapped turns the refusal into an explanation.

Amounts are the API's own, in the currency's minor units: an integer on an invoice, a decimal
string on a price. sdk-python has no billing methods yet, so these routes go through its transport
as plain JSON.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from decimal import Decimal
from typing import Annotated, Any, Literal

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from kaitencloud import (
    APIStatusError,
    ConflictError,
    NotFoundError,
    PermissionDeniedError,
    UnprocessableEntityError,
)
from pydantic import BaseModel, Field

from .. import sdk, shapes, views
from . import DESTRUCTIVE, READ, WRITE

Period = Literal["MONTHLY", "QUARTERLY", "SEMI_ANNUAL", "ANNUAL"]
InvoiceStatus = Literal[
    "DRAFT", "PUSHED", "PUSH_FAILED", "MANUAL", "PAID", "PAYMENT_FAILED", "UNCOLLECTIBLE", "VOID"
]
LIVE = ("TRIAL", "ACTIVE", "PAST_DUE")  # a subscription that bills
MINOR_UNITS = "in the currency's minor units: 4900 EUR is 49.00 EUR"


class NewPrice(BaseModel):
    """A price to add to a License version."""

    billing_model: Literal["FLAT_FEE", "USAGE_BASED", "OVERAGE"] = Field(
        description="FLAT_FEE: an amount per billing period. USAGE_BASED: per unit of a metered "
        "entitlement's usage. OVERAGE: per unit of the usage above the grant"
    )
    unit_amount: Decimal = Field(
        ge=0,
        description=f"Per period for FLAT_FEE, per sale unit for a metered price, {MINOR_UNITS}; "
        "a fraction of a minor unit is allowed",
    )
    currency: str = Field(pattern="^[A-Z]{3}$", description="ISO 4217; one per License version")
    billing_period: Period | None = Field(
        None, description="Required on a FLAT_FEE price, refused on a metered one"
    )
    billing_timing: Literal["ADVANCE", "ARREARS"] | None = Field(
        None, description="A FLAT_FEE price bills ADVANCE unless set; a metered one always ARREARS"
    )
    metered_entitlement: str | None = Field(
        None,
        description="USAGE_BASED and OVERAGE: the entitlement billed, granted by the version, "
        "with a reset period",
    )
    label: str | None = Field(None, max_length=200, description="The invoice line's label")
    is_default: bool | None = Field(
        None, description="Make this FLAT_FEE price the default of its billing period"
    )


class Discount(BaseModel):
    """What a voucher takes off the invoices of the redeeming instance's subscription."""

    percent: Decimal | None = Field(None, gt=0, le=100, description="A percentage off")
    amount: int | None = Field(None, gt=0, description=f"Or a fixed amount, {MINOR_UNITS}")
    currency: str | None = Field(None, pattern="^[A-Z]{3}$", description="With amount")
    applies_to: Literal["LICENSE_BASE", "ADDONS", "BOTH"] = "LICENSE_BASE"


class Boost(BaseModel):
    """What a voucher changes in one entitlement of the redeeming instance, for its duration."""

    modifier: Literal["SET", "ADD", "MULTIPLY", "UNLIMITED"] = Field(
        description="A NUMBER entitlement takes any; a BOOLEAN or CONFIG one, SET only"
    )
    value: Any = Field(
        None, description="A number; true/false or an object for a SET; none for UNLIMITED"
    )


# --- projections ------------------------------------------------------------


def _decimal(value: Decimal | float | str) -> str:
    """A decimal string, as the API takes amounts and quantities: never an exponent."""
    return format(Decimal(str(value)), "f")


def _id(value: str, what: str, where: str) -> str:
    """Prices and invoices have ids, not slugs: checked before one reaches a path."""
    try:
        return str(uuid.UUID(value.strip()))
    except ValueError as e:
        raise ToolError(f"{what} {value!r} is not an id: {where} lists them.") from e


def _price(p: dict) -> dict:
    return shapes.drop_none(
        {
            "id": p["id"],
            "label": p.get("displayLabel"),
            "billing_model": p["billingModel"],
            "billing_period": p.get("billingPeriod"),
            "billing_timing": p.get("billingTiming"),
            "unit_amount": p["unitAmountDecimal"],
            "currency": p["currency"],
            "metered_entitlement": (p.get("metered") or {}).get("entitlementSlug"),
            "is_default": p.get("isDefault") or None,
            "status": p.get("status"),
        }
    )


def _composed(inv: dict) -> dict:
    """An invoice as Kaiten composed it: an issued one, the upcoming one, a preview."""
    return shapes.drop_none(
        {
            "kind": inv["kind"],
            "boundary": shapes.iso(inv.get("boundaryAt")),
            "currency": inv["currency"],
            "subtotal": inv["subtotal"],
            "discount_total": inv.get("discountTotal") or None,
            "total": inv["total"],
            "lines": [
                shapes.drop_none(
                    {
                        "type": line["type"],
                        "label": line.get("label"),
                        "description": line.get("description"),
                        "amount": line["amount"],
                        "service_from": shapes.iso(line.get("serviceFrom")),
                        "service_to": shapes.iso(line.get("serviceTo")),
                        "capped": line.get("capped") or None,
                    }
                )
                for line in inv.get("lines") or []
            ]
            or None,
            "would_hold": inv.get("wouldHold") or None,
        }
    )


def _invoice(inv: dict) -> dict:
    handoff = inv.get("handoffStatus")
    return shapes.drop_none(
        {
            "id": inv["id"],
            "status": inv["status"],
            "instance": inv.get("instanceSlug"),
            "customer": inv.get("customerSlug"),
            "license": inv.get("licenseSlug"),
            "provider": inv.get("providerKind"),
            **_composed(inv),
            "issued_at": shapes.iso(inv.get("issuedAt")),
            "due_at": shapes.iso(inv.get("dueAt")),
            "paid_at": shapes.iso(inv.get("paidAt")),
            "overdue": inv.get("overdue") or None,
            "held": inv.get("holdReason"),
            "handoff": None if handoff == "NOT_REQUIRED" else handoff,
            "external_reference": (inv.get("handoff") or {}).get("externalReference"),
        }
    )


def _subscription(s: dict) -> dict:
    change = s.get("scheduledChange")
    return shapes.drop_none(
        {
            "status": s["status"],
            "base_price": _price(s["basePrice"]),
            "provider": s.get("providerKind"),
            "collection_method": s.get("collectionMethod"),
            "days_until_due": s.get("daysUntilDue"),
            "started_at": shapes.iso(s.get("startedAt")),
            "current_period_start": shapes.iso(s.get("currentPeriodStart")),
            "next_boundary": shapes.iso(s.get("currentPeriodEnd")),
            "trial_ends_at": shapes.iso(s.get("trialEndsAt")),
            "past_due_since": shapes.iso(s.get("pastDueSince")),
            "cancel_at_period_end": s.get("cancelAtPeriodEnd") or None,
            "canceled_at": shapes.iso(s.get("canceledAt")),
            "cancellation_reason": s.get("cancellationReason"),
            "scheduled_change": {
                "license": change["licenseSlug"],
                "price": _price(change["price"]),
                "at": shapes.iso(change["effectiveAt"]),
            }
            if change
            else None,
        }
    )


def _held(a: dict) -> dict:
    return shapes.drop_none(
        {
            "addon": a["addonSlug"],
            "family": a.get("familySlug"),
            "name": a.get("name"),
            "quantity": a["quantity"],
            "max_quantity": a.get("maxQuantity"),
            "pending_quantity": a.get("pendingQuantity"),
            "prices": [_price(p) for p in a.get("prices") or []] or None,
        }
    )


def _family(f: dict) -> dict:
    current = f.get("currentVersion") or {}
    return shapes.drop_none(
        {
            "family": f["slug"],
            "current_version": current.get("slug"),
            "name": current.get("name"),
            "pricing": current.get("pricingType"),
            "max_quantity": current.get("maxQuantity"),
            "versions": {v["slug"]: v["lifecycleState"] for v in f.get("versions") or []},
        }
    )


def _voucher(v: dict) -> dict:
    # A code is a secret that redeems the voucher, and the API returns it to any read:vouchers
    # caller: this server shows its last four characters, the API's own hint, and nothing more.
    return shapes.drop_none(
        {
            "code_hint": v.get("codeHint"),
            "name": v["name"],
            "kind": v["voucherType"],
            "status": v["status"],
            "discount": shapes.drop_none(
                {
                    "type": v.get("priceDiscountType"),
                    "value": v.get("priceDiscountValue"),
                    "currency": v.get("currency"),
                    "applies_to": v.get("priceAppliesTo"),
                }
            )
            or None,
            "boosts": {
                g["entitlementSlug"]: shapes.drop_none(
                    {
                        "modifier": g["modifierType"],
                        "value": g.get("modifierValue") or g.get("value"),
                    }
                )
                for g in v.get("grants") or []
            }
            or None,
            "flag_grants": {g["flagSlug"]: g["variant"] for g in v.get("flagGrants") or []} or None,
            "duration": v.get("duration"),
            "periods": v.get("durationInPeriods"),
            "redemptions": v.get("redemptionsCount"),
            "max_redemptions": v.get("maxRedemptions"),
            "expires_at": shapes.iso(v.get("expiresAt")),
        }
    )


def _redemption(r: dict) -> dict:
    return shapes.drop_none(
        {
            "voucher": r.get("voucherName"),
            "kind": r.get("voucherType"),
            "code_hint": r.get("codeHint"),
            "status": r["status"],
            "redeemed_at": shapes.iso(r.get("redeemedAt")),
            "effective_until": shapes.iso(r.get("effectiveExpiresAt")),
            "invoices_discounted": r.get("applicationsCount") or None,
        }
    )


def _capabilities(c: dict) -> dict:
    if not c.get("enabled"):
        code = {"DEPLOYMENT_DISABLED": "Billing.Disabled", "NOT_ENTITLED": "Billing.NotEntitled"}
        return {
            "enabled": False,
            "reason": c.get("disabledReason"),
            "explanation": sdk.billing_off(code.get(c.get("disabledReason"), "")),
        }
    return {
        "enabled": True,
        "providers": [
            shapes.drop_none(
                {
                    "kind": p["kind"],
                    "connected": p.get("connected"),
                    "livemode": p.get("livemode"),
                    "unavailable": p.get("unavailableReason"),
                }
            )
            for p in c.get("providers") or []
        ],
        "features": sorted(k for k, on in (c.get("features") or {}).items() if on),
    }


# --- reads that compose ----------------------------------------------------


async def _part(call: Awaitable[Any], project: Callable[[Any], Any]) -> Any:
    """A part of a read that the token's scopes, or the release, may not reach: the refusal
    stands in its place rather than failing the whole read."""
    try:
        return project(await call)
    except (PermissionDeniedError, NotFoundError) as e:
        return {"unavailable": sdk.describe(e)}


async def _subscription_of(instance_slug: str) -> dict | None:
    """The instance's subscription, live or canceled; None when it was never subscribed."""
    return await sdk.get_or_none(sdk.call_unwrapped("GET", f"instances/{instance_slug}/billing"))


async def _invoice_by_id(invoice: str) -> dict:
    iid = _id(invoice, "invoice", "kaiten_get_invoices")
    try:
        return await sdk.call_unwrapped("GET", f"invoices/{iid}")
    except NotFoundError as e:
        raise ToolError(f"No invoice {iid}: kaiten_get_invoices lists them.") from e


async def _base_price(license_slug: str, period: str | None, price: str | None) -> dict:
    """The flat fee a subscription bills: an exact price, or its version's default of a period."""
    fees = (
        await sdk.call_unwrapped(
            "GET",
            f"licenses/{license_slug}/prices",
            params={"billingModel": "FLAT_FEE", "status": "ACTIVE"},
        )
        or []
    )
    if price:
        pid = _id(price, "price", "kaiten_get_prices")
        match = next((p for p in fees if p["id"] == pid), None)
    else:
        defaults = [p for p in fees if p.get("isDefault")]
        if period:
            match = next((p for p in defaults if p.get("billingPeriod") == period), None)
        else:
            match = defaults[0] if len(defaults) == 1 else None
    if match is None:
        offered = [
            f"{p['id']} ({p.get('billingPeriod')}{', default' if p.get('isDefault') else ''})"
            for p in fees
        ]
        raise ToolError(
            f"License {license_slug} has no active flat fee matching that. Its flat fees: "
            f"{', '.join(offered) or 'none: add one with kaiten_save_prices'}. Pass billing_period or price."
        )
    return match


def register(mcp: FastMCP, *, writes: bool) -> None:
    @mcp.tool(annotations=READ)
    async def kaiten_get_billing(
        instance: Annotated[
            str | None,
            Field(
                description="An instance slug, for its subscription, upcoming invoice, add-ons, vouchers "
                "and latest invoices"
            ),
        ] = None,
    ) -> dict:
        """Start here for billing. Without an instance: whether billing is on for the organization, and why not;
        who can collect its invoices (no provider, Stripe); its default payment terms; the add-ons it sells, by
        family, with each one's current version; and its vouchers, each shown by the last four characters of its
        code, which is a secret. With an instance: its subscription (status, base price, period and next boundary,
        provider and terms, a scheduled change or cancellation), the invoice its next boundary will issue from its
        usage so far, the add-ons it holds, the vouchers it redeemed, and its latest invoices. Amounts are in the
        currency's minor units: 4900 EUR is 49.00 EUR.
        Scopes: read:billing, with read:addons and read:vouchers for the add-ons and vouchers on sale, and
        read:instances and read:voucher_redemptions for an instance."""
        if not instance:
            caps = _capabilities(await sdk.call_unwrapped("GET", "billing/capabilities"))
            if not caps["enabled"]:
                return {"billing": caps}
            settings, addons, vouchers = await asyncio.gather(
                sdk.call_unwrapped("GET", "billing/settings"),
                _part(
                    sdk.call_unwrapped("GET", "addon-families"),
                    lambda families: [_family(f) for f in families or []],
                ),
                # ponytail: the newest 200 vouchers; page through them when one organization has more.
                _part(
                    sdk.call_unwrapped("GET", "vouchers", params={"limit": 200}),
                    lambda page: [_voucher(v) for v in page["items"]],
                ),
            )
            return {
                "billing": {
                    **caps,
                    "default_collection_method": settings.get("defaultCollectionMethod"),
                    "default_days_until_due": settings.get("defaultDaysUntilDue"),
                },
                "addons": addons,
                "vouchers": vouchers,
            }

        inst = await views.instance(instance)
        base = f"instances/{inst.slug}"
        sub = await _subscription_of(inst.slug)

        async def upcoming_invoice() -> dict | None:
            live = sub is not None and sub["status"] in LIVE
            return (
                await sdk.call_unwrapped("GET", f"{base}/billing/upcoming-invoice")
                if live
                else None
            )

        upcoming, held, redeemed, invoices = await asyncio.gather(
            upcoming_invoice(),
            sdk.call_unwrapped("GET", f"{base}/addons"),
            _part(
                sdk.call_unwrapped("GET", f"{base}/vouchers"),
                lambda rows: [_redemption(r) for r in rows or []],
            ),
            sdk.call_unwrapped("GET", f"{base}/invoices", params={"limit": 5}),
        )
        return shapes.drop_none(
            {
                "instance": inst.slug,
                "customer": inst.customer_slug,
                "license": inst.license_slug,
                "subscription": _subscription(sub) if sub else None,
                "upcoming_invoice": _composed(upcoming) if upcoming else None,
                "addons": [_held(a) for a in held or []],
                "vouchers": redeemed,
                "latest_invoices": [_invoice(i) for i in invoices["items"]],
                "notes": ["never subscribed: kaiten_subscribe_instance starts billing it"]
                if sub is None
                else None,
            }
        )

    @mcp.tool(annotations=READ)
    async def kaiten_get_prices(
        plan: Annotated[
            str | None, Field(description="A plan (license family slug): its current version")
        ] = None,
        license: Annotated[
            str | None, Field(description="An exact license version slug, a draft included")
        ] = None,
        include_deprecated: bool = False,
        preview: Annotated[
            bool,
            Field(
                description="Also compose the invoice a subscription to the version would be billed now, "
                "writing nothing. Implied by any of the arguments below"
            ),
        ] = False,
        sample_usage: Annotated[
            dict[str, Decimal] | None,
            Field(
                description="Usage of each metered entitlement over the period, e.g. {'monthly-orders': 106}; "
                "usage past what the grant accepts is capped"
            ),
        ] = None,
        instance: Annotated[
            str | None, Field(description="Rate this instance's own usage instead of a sample")
        ] = None,
        add_ons: Annotated[
            dict[str, int] | None,
            Field(description="Add-on version slugs and quantities to price in"),
        ] = None,
        voucher_code: Annotated[
            str | None, Field(description="A voucher whose discount to apply; nothing is redeemed")
        ] = None,
        base_price: Annotated[
            str | None,
            Field(
                description="The flat fee billed as the base (a price id); the version's first default"
            ),
        ] = None,
    ) -> dict:
        """What a License version costs: its prices -- flat fees per billing period, usage-based and overage prices
        on a metered entitlement -- with the ids the other billing tools take; and, with preview or any preview
        argument, the renewal invoice a subscription to it would be billed at a boundary now, from a usage sample or
        an instance's own usage, with add-ons and a voucher. Works on a draft, before its prices freeze. Amounts are
        in the currency's minor units: 4900 EUR is 49.00 EUR. Scopes: read:licenses."""
        lic = await views.resolve_license(plan=plan, license=license)
        prices = await sdk.call_unwrapped(
            "GET",
            f"licenses/{lic.slug}/prices",
            params=None if include_deprecated else {"status": "ACTIVE"},
        )
        out: dict[str, Any] = {
            "license": lic.slug,
            "state": lic.lifecycle_state,
            "prices": [_price(p) for p in prices or []],
        }
        scenario = (sample_usage, instance, add_ons, voucher_code, base_price)
        if preview or any(arg is not None for arg in scenario):
            if sample_usage and instance:
                raise ToolError("Pass sample_usage or instance, not both.")
            body = shapes.drop_none(
                {
                    "basePriceId": _id(base_price, "base_price", "kaiten_get_prices")
                    if base_price
                    else None,
                    "sampleUsage": [
                        {"entitlementSlug": shapes.slug(e, "entitlement"), "quantity": _decimal(q)}
                        for e, q in (sample_usage or {}).items()
                    ]
                    or None,
                    "instanceSlug": shapes.slug(instance, "instance") if instance else None,
                    "addOns": [
                        {"addonSlug": shapes.slug(a, "add-on"), "quantity": n}
                        for a, n in (add_ons or {}).items()
                    ]
                    or None,
                    "voucherCode": voucher_code,
                }
            )
            out["preview"] = _composed(
                await sdk.call_unwrapped(
                    "POST", f"licenses/{lic.slug}/invoice-preview", body, idempotent=True
                )
            )
        return out

    @mcp.tool(annotations=READ)
    async def kaiten_get_invoices(
        invoice: Annotated[
            str | None, Field(description="An invoice id, for that invoice with its lines")
        ] = None,
        view: Annotated[
            Literal["all", "overdue", "held", "handoff", "acknowledged"],
            Field(
                description="overdue: issued, unpaid past its due date. held: a draft held for its usage "
                "journal. handoff: issued with no payment provider, waiting for the organization's accounting "
                "system. acknowledged: taken by that system"
            ),
        ] = "all",
        customer: Annotated[str | None, Field(description="Only this customer's")] = None,
        instance: Annotated[str | None, Field(description="Only this instance's")] = None,
        status: Annotated[
            list[InvoiceStatus] | None,
            Field(description="Only these statuses. MANUAL: issued, for you to collect"),
        ] = None,
        limit: Annotated[int, Field(ge=1, le=200)] = 25,
    ) -> dict:
        """The organization's invoices, newest first, in the console's views: all, overdue, held, the handoff queue
        and what the accounting system acknowledged. With an invoice id: that invoice with its lines, each with the
        arithmetic Kaiten composed it from. Amounts are in the currency's minor units: 4900 EUR is 49.00 EUR.
        Scopes: read:billing."""
        if invoice:
            return _invoice(await _invoice_by_id(invoice))
        page = await sdk.call_unwrapped(
            "GET",
            "invoices",
            params={
                "overdue": view == "overdue" or None,
                "held": view == "held" or None,
                "handoffStatus": {"handoff": "PENDING", "acknowledged": "ACKNOWLEDGED"}.get(view),
                "customerSlug": shapes.slug(customer, "customer") if customer else None,
                "instanceSlug": shapes.slug(instance, "instance") if instance else None,
                "status": status,
                "limit": limit,
            },
        )
        return shapes.drop_none(
            {
                "view": view,
                "invoices": [_invoice(i) for i in page["items"]],
                "more": page.get("hasMore") or None,
            }
        )

    if not writes:
        return

    @mcp.tool(annotations=WRITE)
    async def kaiten_save_prices(
        license: Annotated[str, Field(description="License version slug")],
        add: Annotated[list[NewPrice] | None, Field(description="Prices to add")] = None,
        deprecate: Annotated[
            list[str] | None,
            Field(description="Ids of prices to retire for good, from kaiten_get_prices"),
        ] = None,
    ) -> dict:
        """Price a License version: add prices and deprecate others; prices not mentioned are kept. A price is never
        edited: a deprecated one stops being offered and stops being a default, while subscriptions pinned to it keep
        being billed from it. Deprecations go first, so a new default can replace an old one. Once a subscription
        bills the version, Kaiten refuses new prices: create a new version (kaiten_save_license new_version_of=...).
        A price identical to an active one reports 'unchanged'. Scopes: write:licenses."""
        lic = await views.resolve_license(plan=None, license=license)
        path = f"licenses/{lic.slug}/prices"
        current = await sdk.call_unwrapped("GET", path) or []
        by_id = {p["id"]: p for p in current}
        done: dict[str, list] = {"deprecated": [], "added": []}
        unchanged: list[str] = []

        def same(p: dict, n: NewPrice) -> bool:
            return (
                p["status"] == "ACTIVE"
                and p["billingModel"] == n.billing_model
                and p.get("billingPeriod") == n.billing_period
                and p["currency"] == n.currency
                and Decimal(p["unitAmountDecimal"]) == n.unit_amount
                and (p.get("metered") or {}).get("entitlementSlug") == n.metered_entitlement
                and n.billing_timing in (None, p.get("billingTiming"))
            )

        retire = [_id(raw, "price", "kaiten_get_prices") for raw in deprecate or []]
        missing = [pid for pid in retire if pid not in by_id]
        if missing:
            raise ToolError(f"License {lic.slug} has no price {', '.join(missing)}.")
        try:
            for pid in retire:
                if by_id[pid]["status"] == "DEPRECATED":
                    unchanged.append(f"{pid} was already deprecated")
                    continue
                retired = await sdk.call_unwrapped("POST", f"{path}/{pid}/deprecate")
                by_id[pid].update(retired)  # no longer counts as an active twin of a new price
                done["deprecated"].append(_price(retired))
            for n in add or []:
                if any(same(p, n) for p in current):
                    unchanged.append(f"an identical {n.billing_model} price is already active")
                    continue
                body = shapes.drop_none(
                    {
                        "billingModel": n.billing_model,
                        "unitAmountDecimal": _decimal(n.unit_amount),
                        "currency": n.currency,
                        "billingPeriod": n.billing_period,
                        "billingTiming": n.billing_timing,
                        "meteredEntitlementSlug": shapes.slug(n.metered_entitlement, "entitlement")
                        if n.metered_entitlement
                        else None,
                        "displayLabel": n.label,
                        "isDefault": n.is_default,
                    }
                )
                created = await sdk.call_unwrapped("POST", path, body)
                current.append(created)
                done["added"].append(_price(created))
        except APIStatusError as e:
            # Report what was written before the refusal, rather than an error that hides it.
            failed = sdk.describe(e)
        else:
            failed = None
        changes = {k: v for k, v in done.items() if v}
        after = [
            _price(p)
            for p in await sdk.call_unwrapped("GET", path, params={"status": "ACTIVE"}) or []
        ]
        status = "updated" if changes else "rejected" if failed else "unchanged"
        return shapes.report(
            status,
            "pricing",
            {"license": lic.slug, "state": lic.lifecycle_state, "prices": after},
            changes=changes,
            reason=failed,
            notes=unchanged
            + (
                ["stopped at the refusal: what is listed under changes was written"]
                if failed and changes
                else []
            ),
        )

    @mcp.tool(annotations={**WRITE, "idempotentHint": False})
    async def kaiten_subscribe_instance(
        instance: Annotated[
            str, Field(description="Instance slug; its License version must be published")
        ],
        billing_period: Annotated[
            Period | None,
            Field(description="Bill the default flat fee of this period of the instance's version"),
        ] = None,
        price: Annotated[
            str | None,
            Field(
                description="Or this exact flat fee of its version: a price id from kaiten_get_prices"
            ),
        ] = None,
        provider: Annotated[
            Literal["NOOP", "STRIPE"],
            Field(
                description="Who collects the invoices. NOOP: no provider, the organization's own accounting "
                "system takes them from the handoff queue. STRIPE: must be connected"
            ),
        ] = "NOOP",
        collection_method: Annotated[
            Literal["SEND_INVOICE", "CHARGE_AUTOMATICALLY"] | None,
            Field(
                description="CHARGE_AUTOMATICALLY needs Stripe and a payment method; the organization's default "
                "when omitted"
            ),
        ] = None,
        days_until_due: Annotated[
            int | None,
            Field(
                ge=0, le=365, description="Payment terms; the organization's default when omitted"
            ),
        ] = None,
        trial_days: Annotated[
            int | None,
            Field(ge=0, le=365, description="A trial, 0 for none; the version's own when omitted"),
        ] = None,
        start_at: Annotated[
            str | None,
            Field(description="ISO date the periods count from: now, or up to one period back"),
        ] = None,
        add_ons: Annotated[
            dict[str, int] | None,
            Field(description="Add-on version slugs and quantities to attach with it"),
        ] = None,
        voucher_code: Annotated[
            str | None, Field(description="A voucher to redeem with it, from its first invoice")
        ] = None,
    ) -> dict:
        """Start billing an instance: its subscription is pinned to a flat fee of its License version -- the default of
        a billing period, or an exact price -- and its invoices are collected by no provider (the handoff queue) or
        by Stripe. A base billed in advance issues the first period's invoice at once, in the answer. While the
        subscription lives, the instance's customer and License cannot change. An instance already billed on that
        price reports 'unchanged'. Scopes: write:billing, read:instances, read:licenses."""
        if price and billing_period:
            raise ToolError("Pass billing_period or price, not both.")
        inst = await views.instance(instance)
        current = await _subscription_of(inst.slug)
        base = await _base_price(inst.license_slug, billing_period, price)
        if current and current["status"] in LIVE:
            if current["basePrice"]["id"] == base["id"]:
                return shapes.report(
                    "unchanged", "subscription", _subscription(current), instance=inst.slug
                )
            raise ToolError(
                f"Instance {inst.slug} is already billed ({current['status']}) on "
                f"{current['basePrice'].get('displayLabel') or current['basePrice']['id']}. A move to another "
                "price applies at its next boundary, from the console; or cancel it first with "
                "kaiten_cancel_subscription."
            )
        body = shapes.drop_none(
            {
                "basePriceId": base["id"],
                "providerKind": provider,
                "collectionMethod": collection_method,
                "daysUntilDue": days_until_due,
                "trialDays": trial_days,
                "startAt": shapes.rfc3339(shapes.parse_time(start_at, "start_at"))
                if start_at
                else None,
                "addOns": [
                    {"addonSlug": shapes.slug(a, "add-on"), "quantity": n}
                    for a, n in (add_ons or {}).items()
                ]
                or None,
                "voucherCode": voucher_code,
            }
        )
        started = await sdk.call_unwrapped("POST", f"instances/{inst.slug}/billing", body)
        activation = started.get("activationInvoice")
        return shapes.report(
            "created",
            "subscription",
            _subscription(started),
            instance=inst.slug,
            activation_invoice=_invoice(activation) if activation else None,
            notes=[
                "with no provider, its invoices wait in the handoff queue (kaiten_get_invoices view=handoff); "
                "record a payment with kaiten_mark_invoice_paid"
            ]
            if provider == "NOOP"
            else None,
        )

    @mcp.tool(annotations=DESTRUCTIVE)
    async def kaiten_cancel_subscription(
        instance: Annotated[str, Field(description="Instance slug")],
        when: Annotated[
            Literal["period_end", "now"],
            Field(
                description="period_end: the period paid for runs out, then a final invoice bills what is left "
                "in arrears; it can be reactivated until then, from the console. now: the final invoice is issued "
                "at once, fees billed in arrears in full, nothing refunded, and it cannot be undone"
            ),
        ] = "period_end",
        reason: Annotated[str | None, Field(max_length=500)] = None,
    ) -> dict:
        """Cancel an instance's subscription. It ends billing only: the instance keeps its add-ons, vouchers and
        license dates, and keeps its License -- unless its License family names a fallback, which the instance moves
        to when the subscription ends. A trial ends at once, with no invoice. A cancellation already scheduled or
        done reports 'unchanged'. Scopes: write:billing, read:instances."""
        inst = await views.instance(instance)
        cur = await _subscription_of(inst.slug)
        if cur is None:
            raise ToolError(
                f"Instance {inst.slug} was never subscribed: there is nothing to cancel."
            )
        if cur["status"] == "CANCELED" or (when == "period_end" and cur.get("cancelAtPeriodEnd")):
            return shapes.report(
                "unchanged", "subscription", _subscription(cur), instance=inst.slug
            )
        after = await sdk.call_unwrapped(
            "POST",
            f"instances/{inst.slug}/billing/cancel",
            shapes.drop_none(
                {"mode": "IMMEDIATE" if when == "now" else "AT_PERIOD_END", "reason": reason}
            ),
        )
        notes = []
        if after["status"] == "CANCELED":
            moved = (await sdk.kaiten().instances.get(inst.slug)).license_slug
            if moved != inst.license_slug:
                notes.append(f"the instance fell back from {inst.license_slug} to {moved}")
        else:
            notes.append(
                "if its License family names a fallback, the instance moves to it at the boundary"
            )
        final = after.get("finalInvoice")
        return shapes.report(
            "updated",
            "subscription",
            _subscription(after),
            instance=inst.slug,
            changes=shapes.changes(_subscription(cur), _subscription(after)),
            final_invoice=_invoice(final) if final else None,
            notes=notes,
        )

    @mcp.tool(annotations=DESTRUCTIVE)
    async def kaiten_set_instance_addon(
        instance: Annotated[str, Field(description="Instance slug")],
        addon: Annotated[
            str,
            Field(
                description="The add-on version slug to attach (kaiten_get_billing lists each family's current "
                "version); for one the instance holds, its version or family slug"
            ),
        ],
        quantity: Annotated[int, Field(ge=0, description="Units to hold; 0 detaches it")],
    ) -> dict:
        """Attach an add-on to an instance, change how many units it holds, or detach it (quantity 0). Its
        entitlements follow at once, raising or lowering the instance's effective limits. A subscription bills the
        quantity held at each boundary, without proration; a decrease or a detach refunds nothing. An instance holds
        one version per family. Scopes: write:instances."""
        islug, aslug = shapes.slug(instance, "instance"), shapes.slug(addon, "add-on")
        path = f"instances/{islug}/addons"
        held = await sdk.call_unwrapped("GET", path) or []
        cur = next((a for a in held if aslug in (a["addonSlug"], a["familySlug"])), None)
        if cur is None and quantity == 0:
            return shapes.report(
                "unchanged", "addon", {"instance": islug, "addon": aslug, "quantity": 0}
            )
        if cur is None:
            created = await sdk.call_unwrapped(
                "POST", path, {"addonSlug": aslug, "quantity": quantity}
            )
            return shapes.report("created", "addon", {"instance": islug, **_held(created)})
        if quantity == 0:
            await sdk.call_unwrapped("DELETE", f"{path}/{cur['addonSlug']}")
            return shapes.report(
                "deleted",
                "addon",
                {"instance": islug, **_held(cur)},
                notes=["its entitlements dropped at once; nothing is refunded"],
            )
        if cur["quantity"] == quantity and cur.get("pendingQuantity") is None:
            return shapes.report("unchanged", "addon", {"instance": islug, **_held(cur)})
        after = await sdk.call_unwrapped(
            "PATCH", f"{path}/{cur['addonSlug']}", {"quantity": quantity}, idempotent=True
        )
        return shapes.report(
            "updated",
            "addon",
            {"instance": islug, **_held(after)},
            changes=shapes.changes(
                {"quantity": cur["quantity"], "pending_quantity": cur.get("pendingQuantity")},
                {"quantity": after["quantity"], "pending_quantity": after.get("pendingQuantity")},
            ),
        )

    @mcp.tool(annotations=WRITE)
    async def kaiten_save_voucher(
        code: Annotated[
            str,
            Field(
                pattern="^[A-Za-z0-9_-]{8,64}$",
                description="The code to redeem, matched without case or separators. Under 12 characters "
                "(separators left out) it needs max_redemptions or expires_at",
            ),
        ],
        name: Annotated[str | None, Field(description="Display name (required to create)")] = None,
        discount: Annotated[
            Discount | None, Field(description="Off the invoices of the redeeming instance")
        ] = None,
        boosts: Annotated[
            dict[str, Boost] | None,
            Field(
                description="Entitlement slug -> what the voucher changes in it, for its duration"
            ),
        ] = None,
        flag_grants: Annotated[
            dict[str, str] | None,
            Field(description="Feature flag slug -> the variant it serves the redeeming instance"),
        ] = None,
        duration: Annotated[
            Literal["ONE_TIME", "REPEATING", "FOREVER"],
            Field(
                description="A discount counts invoices, a boost or flag grant billing periods: one, periods, or "
                "without end"
            ),
        ] = "ONE_TIME",
        periods: Annotated[int | None, Field(ge=1, description="With REPEATING")] = None,
        max_redemptions: Annotated[int | None, Field(ge=1)] = None,
        expires_at: Annotated[str | None, Field(description="ISO date or timestamp")] = None,
        licenses: Annotated[
            list[str] | None,
            Field(description="Only instances on these License version slugs may redeem it"),
        ] = None,
        customer: Annotated[
            str | None, Field(description="Only this customer's instances may redeem it")
        ] = None,
        description: str | None = None,
        publish: Annotated[
            bool, Field(description="Make it redeemable now; false keeps it a DRAFT")
        ] = True,
    ) -> dict:
        """Create a voucher, a code an instance redeems: a discount on its invoices, a boost of its entitlements, a
        feature flag variant served to it, or several at once -- its kind follows from what you pass. It is created
        as a draft, then published unless publish is false. For a code that exists: publishes it if it is a draft
        and publish is true, else reports 'unchanged'; it never edits one. The answer that creates the voucher
        repeats its code; every other answer shows its last four characters. Scopes: write:vouchers, and
        read:licenses for licenses."""
        k = sdk.kaiten()
        try:
            cur = await sdk.call_unwrapped(
                "POST", "vouchers/lookup", {"code": code}, idempotent=True
            )
        except NotFoundError:
            cur = None
        if cur is not None:
            if not (publish and cur["status"] == "DRAFT"):
                return shapes.report("unchanged", "voucher", _voucher(cur))
            after = await sdk.call_unwrapped("POST", f"vouchers/{cur['id']}/publish")
            return shapes.report(
                "updated",
                "voucher",
                _voucher(after),
                changes={"status": {"from": "DRAFT", "to": after["status"]}},
            )
        parts = [
            p
            for p, given in (
                ("PRICE", discount),
                ("ENTITLEMENT_BOOST", boosts),
                ("FLAG_GRANT", flag_grants),
            )
            if given
        ]
        if not name or not parts:
            raise ToolError(
                f"No voucher has the code {code!r} yet: pass a name, and a discount, boosts or flag_grants, to create it."
            )
        if discount and (discount.percent is None) == (discount.amount is None):
            raise ToolError("A discount is a percent, or an amount with its currency.")
        body = shapes.drop_none(
            {
                "code": code,
                "name": name,
                "description": description,
                "voucherType": parts[0] if len(parts) == 1 else "COMPOSITE",
                "duration": duration,
                "durationInPeriods": periods,
                "maxRedemptions": max_redemptions,
                "expiresAt": shapes.rfc3339(shapes.parse_time(expires_at, "expires_at"))
                if expires_at
                else None,
                "restrictedCustomerSlug": shapes.slug(customer, "customer") if customer else None,
                "applicableLicenseIds": [
                    (
                        await sdk.require(
                            k.licenses.get(shapes.slug(s, "license")), f"license {s!r}"
                        )
                    ).id
                    for s in licenses or []
                ]
                or None,
            }
        )
        if discount:
            body |= shapes.drop_none(
                {
                    "priceDiscountType": "PERCENTAGE"
                    if discount.percent is not None
                    else "FIXED_AMOUNT",
                    "priceDiscountValue": _decimal(
                        discount.percent if discount.percent is not None else discount.amount
                    ),
                    "currency": discount.currency,
                    "priceAppliesTo": discount.applies_to,
                }
            )
        if boosts:
            body["grants"] = [
                shapes.drop_none(
                    {
                        "entitlementSlug": shapes.slug(e, "entitlement"),
                        "modifierType": b.modifier,
                        "modifierValue": _decimal(b.value)
                        if isinstance(b.value, int | float) and not isinstance(b.value, bool)
                        else None,
                        "value": b.value if isinstance(b.value, bool | dict) else None,
                    }
                )
                for e, b in boosts.items()
            ]
        if flag_grants:
            body["flagGrants"] = [
                {"flagSlug": shapes.slug(f, "flag"), "variant": v} for f, v in flag_grants.items()
            ]
        created = await sdk.call_unwrapped("POST", "vouchers", body)
        if publish:
            created = await sdk.call_unwrapped("POST", f"vouchers/{created['id']}/publish")
        return shapes.report("created", "voucher", {"code": code, **_voucher(created)})

    @mcp.tool(annotations=WRITE)
    async def kaiten_redeem_voucher(
        instance: Annotated[str, Field(description="Instance slug")],
        code: Annotated[str, Field(description="The voucher's code")],
    ) -> dict:
        """Redeem a voucher for an instance: a discount applies to its subscription's next invoices, a boost or a flag
        grant from the next read or evaluation, each for the voucher's duration. A code the instance cannot redeem --
        unknown, expired, exhausted, not eligible -- comes back 'rejected' with the reason; one it already redeemed,
        'unchanged'. Scopes: write:voucher_redemptions."""
        islug = shapes.slug(instance, "instance")
        try:
            r = await sdk.call_unwrapped(
                "POST", f"instances/{islug}/vouchers/redeem", {"code": code}
            )
        except (ConflictError, UnprocessableEntityError, NotFoundError) as e:
            if e.code == "RedeemVoucher.InstanceNotFound":
                raise
            status = "unchanged" if (e.code or "").endswith("AlreadyRedeemed") else "rejected"
            return shapes.report(
                status, "redemption", {"instance": islug}, reason=f"{e.code}: {e.detail}"
            )
        return shapes.report("created", "redemption", {"instance": islug, **_redemption(r)})

    @mcp.tool(annotations=WRITE)
    async def kaiten_mark_invoice_paid(
        invoice: Annotated[str, Field(description="Invoice id, from kaiten_get_invoices")],
        external_reference: Annotated[
            str | None,
            Field(
                min_length=1,
                max_length=255,
                description="Its number in your accounting system; also acknowledges it from the handoff queue",
            ),
        ] = None,
        paid_at: Annotated[
            str | None, Field(description="ISO date or timestamp, now or earlier; now when omitted")
        ] = None,
        note: Annotated[
            str | None,
            Field(
                max_length=1000,
                description="E.g. the transfer's reference; kept in the paid event only",
            ),
        ] = None,
    ) -> dict:
        """Record that an invoice the organization collects itself -- no payment provider, status MANUAL -- was paid.
        One a payment provider collects is settled there. A paid invoice reports 'unchanged'.
        Scopes: write:billing."""
        cur = await _invoice_by_id(invoice)
        reference = (cur.get("handoff") or {}).get("externalReference")
        if cur["status"] == "PAID" and external_reference in (None, reference):
            return shapes.report("unchanged", "invoice", _invoice(cur))
        after = await sdk.call_unwrapped(
            "POST",
            f"invoices/{cur['id']}/mark-paid",
            shapes.drop_none(
                {
                    "externalReference": external_reference,
                    "paidAt": shapes.rfc3339(shapes.parse_time(paid_at, "paid_at"))
                    if paid_at
                    else None,
                    "note": note,
                }
            ),
        )
        return shapes.report(
            "updated",
            "invoice",
            _invoice(after),
            changes=shapes.changes(_invoice(cur), _invoice(after)),
        )
