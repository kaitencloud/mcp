# Copyright 2026 KAITEN INC
# SPDX-License-Identifier: Apache-2.0

"""Billing: what a License version costs, a plan's settings for selling it (its public listing, its
fallback plan, how its trials end), the subscription that bills an instance, its invoices, and the
add-ons and vouchers on top of its License.

Kaiten composes every invoice, from the prices and the usage journal; Stripe, or the
organization's own accounting system through the handoff queue, collects it. Billing is off until
it is switched on, and its routes refuse every call until then: kaiten_get_billing says why, and
every billing tool answers such a refusal with the same explanation.

Amounts are the API's own, in the currency's minor units: an integer on an invoice, a decimal
string on a price.
"""

from __future__ import annotations

import asyncio
import functools
import uuid
from collections.abc import Awaitable, Callable
from decimal import Decimal
from typing import Annotated, Any, Literal

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from kaitencloud import (
    APIStatusError,
    ConflictError,
    KaitenError,
    NotFoundError,
    PermissionDeniedError,
    UnprocessableEntityError,
)
from kaitencloud.types import (
    AddonFamily,
    BillingCapabilities,
    BillingDisabledReason,
    BillingModel,
    BillingPeriod,
    BillingProviderKind,
    BillingTiming,
    CanceledSubscription,
    CollectionMethod,
    GrantModifier,
    InstanceAddon,
    InstanceBilling,
    Invoice,
    InvoicePreview,
    InvoiceStatus,
    InvoiceSummary,
    Price,
    Redemption,
    StartedSubscription,
    TrialEndWithoutPaymentMethod,
    Voucher,
    VoucherDuration,
)
from pydantic import BaseModel, Field

from .. import sdk, shapes, views
from . import DESTRUCTIVE, READ, WRITE

LIVE = ("TRIAL", "ACTIVE", "PAST_DUE")  # a subscription that bills
MINOR_UNITS = "in the currency's minor units: 4900 EUR is 49.00 EUR"

# What billing being off means for the agent, keyed by the reason GET /billing/capabilities gives
# (the SDK's BillingDisabledReason). A refusal is explained from there, never from its error code,
# which no contract lists.
BILLING_OFF: dict[BillingDisabledReason, str] = {
    "DEPLOYMENT_DISABLED": "Billing is off on this Kaiten installation: a self-hosted operator "
    "turns it on with KAITEN_BILLING_ENABLED.",
    "NOT_ENTITLED": "Billing is not included in this organization's Kaiten plan.",
}


def billing_off(reason: str | None) -> str:
    return (
        f"{BILLING_OFF.get(reason, 'Billing is off.')} Licenses, entitlements, usage and entitlement "
        "exceptions work without it; prices, subscriptions, invoices, add-ons and vouchers need it."
    )


class NewPrice(BaseModel):
    """A price to add to a License version."""

    billing_model: BillingModel = Field(
        description="FLAT_FEE: an amount per billing period. USAGE_BASED: per unit of a metered "
        "entitlement's usage. OVERAGE: per unit of the usage above the grant"
    )
    unit_amount: Decimal = Field(
        ge=0,
        description=f"Per period for FLAT_FEE, per sale unit for a metered price, {MINOR_UNITS}; "
        "a fraction of a minor unit is allowed",
    )
    currency: str = Field(pattern="^[A-Z]{3}$", description="ISO 4217; one per License version")
    billing_period: BillingPeriod | None = Field(
        None, description="Required on a FLAT_FEE price, refused on a metered one"
    )
    billing_timing: BillingTiming | None = Field(
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

    modifier: GrantModifier = Field(
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


def _keyed(values: dict[str, Any] | None, what: str) -> dict[str, Any] | None:
    """Arguments keyed by slug, each slug checked; None for none, which the SDK leaves out."""
    return {shapes.slug(key, what): value for key, value in values.items()} if values else None


def _price(p: Price) -> dict:
    return shapes.drop_none(
        {
            "id": p.id,
            "label": p.display_label,
            "billing_model": p.billing_model,
            "billing_period": p.billing_period,
            "billing_timing": p.billing_timing,
            "unit_amount": p.unit_amount_decimal,
            "currency": p.currency,
            "metered_entitlement": p.metered.entitlement_slug if p.metered else None,
            "is_default": p.is_default or None,
            "status": p.status,
        }
    )


def _composed(inv: Invoice | InvoiceSummary | InvoicePreview) -> dict:
    """An invoice as Kaiten composed it: an issued one, the upcoming one, a preview. Lists and
    writes answer an issued one as a summary, without its lines."""
    lines = [] if isinstance(inv, InvoiceSummary) else inv.lines
    holds = inv.would_hold if isinstance(inv, InvoicePreview) else []
    return shapes.drop_none(
        {
            "kind": inv.kind,
            "boundary": shapes.iso(inv.boundary_at),
            "currency": inv.currency,
            "subtotal": inv.subtotal,
            "discount_total": inv.discount_total or None,
            "total": inv.total,
            "lines": [
                shapes.drop_none(
                    {
                        "type": line.type,
                        "label": line.label,
                        "description": line.description,
                        "amount": line.amount,
                        "service_from": shapes.iso(line.service_from),
                        "service_to": shapes.iso(line.service_to),
                        "capped": line.capped or None,
                    }
                )
                for line in lines
            ]
            or None,
            "would_hold": [hold.to_dict() for hold in holds] or None,
        }
    )


def _invoice(inv: Invoice | InvoiceSummary) -> dict:
    handoff = inv.handoff_status
    return shapes.drop_none(
        {
            "id": inv.id,
            "status": inv.status,
            "instance": inv.instance_slug,
            "customer": inv.customer_slug,
            "license": inv.license_slug,
            "provider": inv.provider_kind,
            **_composed(inv),
            "issued_at": shapes.iso(inv.issued_at),
            "due_at": shapes.iso(inv.due_at),
            "paid_at": shapes.iso(inv.paid_at),
            "overdue": inv.overdue or None,
            "held": inv.hold_reason,
            "handoff": None if handoff == "NOT_REQUIRED" else handoff,
            "external_reference": inv.handoff.external_reference
            if isinstance(inv, Invoice)
            else None,
        }
    )


def _subscription(s: InstanceBilling | StartedSubscription | CanceledSubscription) -> dict:
    change = s.scheduled_change
    return shapes.drop_none(
        {
            "status": s.status,
            "base_price": _price(s.base_price),
            "provider": s.provider_kind,
            "collection_method": s.collection_method,
            "days_until_due": s.days_until_due,
            "started_at": shapes.iso(s.started_at),
            "current_period_start": shapes.iso(s.current_period_start),
            "next_boundary": shapes.iso(s.current_period_end),
            "trial_ends_at": shapes.iso(s.trial_ends_at),
            "past_due_since": shapes.iso(s.past_due_since),
            "cancel_at_period_end": s.cancel_at_period_end or None,
            "canceled_at": shapes.iso(s.canceled_at),
            "cancellation_reason": s.cancellation_reason,
            "scheduled_change": {
                "license": change.license_slug,
                "price": _price(change.price),
                "at": shapes.iso(change.effective_at),
            }
            if change
            else None,
        }
    )


def _held(a: InstanceAddon) -> dict:
    return shapes.drop_none(
        {
            "addon": a.addon_slug,
            "family": a.family_slug,
            "name": a.name,
            "quantity": a.quantity,
            "max_quantity": a.max_quantity,
            "pending_quantity": a.pending_quantity,
            "prices": [_price(p) for p in a.prices] or None,
        }
    )


def _family(f: AddonFamily) -> dict:
    current = f.current_version
    return shapes.drop_none(
        {
            "family": f.slug,
            **(
                {
                    "current_version": current.slug,
                    "name": current.name,
                    "pricing": current.pricing_type,
                    "max_quantity": current.max_quantity,
                }
                if current
                else {}
            ),
            "versions": {v.slug: v.lifecycle_state for v in f.versions or []},
        }
    )


def _voucher(v: Voucher) -> dict:
    # A code is a secret that redeems the voucher, and the API returns it to any read:vouchers
    # caller: this server shows its last four characters, the API's own hint, and nothing more.
    return shapes.drop_none(
        {
            "code_hint": v.code_hint,
            "name": v.name,
            "kind": v.voucher_type,
            "status": v.status,
            "discount": shapes.drop_none(
                {
                    "type": v.price_discount_type,
                    "value": v.price_discount_value,
                    "currency": v.currency,
                    "applies_to": v.price_applies_to,
                }
            )
            or None,
            "boosts": {
                g.entitlement_slug: shapes.drop_none(
                    {"modifier": g.modifier_type, "value": g.modifier_value or g.value}
                )
                for g in v.grants
            }
            or None,
            "flag_grants": {g.flag_slug: g.variant for g in v.flag_grants} or None,
            "duration": v.duration,
            "periods": v.duration_in_periods,
            "redemptions": v.redemptions_count,
            "max_redemptions": v.max_redemptions,
            "expires_at": shapes.iso(v.expires_at),
        }
    )


def _redemption(r: Redemption) -> dict:
    return shapes.drop_none(
        {
            "voucher": r.voucher_name,
            "kind": r.voucher_type,
            "code_hint": r.code_hint,
            "status": r.status,
            "redeemed_at": shapes.iso(r.redeemed_at),
            "effective_until": shapes.iso(r.effective_expires_at),
            "invoices_discounted": r.applications_count or None,
        }
    )


def _capabilities(c: BillingCapabilities) -> dict:
    if not c.enabled:
        return {
            "enabled": False,
            "reason": c.disabled_reason,
            "explanation": billing_off(c.disabled_reason),
        }
    return {
        "enabled": True,
        "providers": [
            shapes.drop_none(
                {
                    "kind": p.kind,
                    "connected": p.connected,
                    "livemode": p.livemode,
                    "unavailable": p.unavailable_reason,
                }
            )
            for p in c.providers
        ],
        # By their names on the wire, as the API lists them.
        "features": sorted(k for k, on in c.features.to_dict().items() if on),
    }


def _explained(tool: Callable[..., Awaitable[Any]]) -> Callable[..., Awaitable[Any]]:
    """A billing tool that answers a refusal where billing is off with why, as the organization's
    capabilities say, and what works without it. Any other refusal stands as Kaiten gave it."""

    @functools.wraps(tool)
    async def run(*args: Any, **kwargs: Any) -> Any:
        try:
            return await tool(*args, **kwargs)
        except PermissionDeniedError as e:
            try:
                caps = await sdk.kaiten().billing.get_capabilities()
            except KaitenError:
                caps = None
            if caps is None or caps.enabled:
                raise
            # The tool's own error: the agent reads why, and the server logs no traceback.
            raise ToolError(f"{billing_off(caps.disabled_reason)} ({e.code})") from e

    return run


# --- reads that compose ----------------------------------------------------


async def _part(call: Awaitable[Any], project: Callable[[Any], Any]) -> Any:
    """A part of a read that the token's scopes, or the release, may not reach: the refusal
    stands in its place rather than failing the whole read."""
    try:
        return project(await call)
    except (PermissionDeniedError, NotFoundError) as e:
        return {"unavailable": sdk.describe(e)}


async def _subscription_of(instance_slug: str) -> InstanceBilling | None:
    """The instance's subscription, live or canceled; None when it was never subscribed."""
    return await sdk.get_or_none(sdk.kaiten().billing.subscriptions.get(instance_slug))


async def _invoice_by_id(invoice: str) -> Invoice:
    iid = _id(invoice, "invoice", "kaiten_get_invoices")
    try:
        return await sdk.kaiten().billing.invoices.get(iid)
    except NotFoundError as e:
        raise ToolError(f"No invoice {iid}: kaiten_get_invoices lists them.") from e


async def _base_price(license_slug: str, period: str | None, price: str | None) -> Price:
    """The flat fee a subscription bills: an exact price, or its version's default of a period."""
    fees = await sdk.kaiten().billing.license_prices.list(
        license_slug, status="ACTIVE", billing_model="FLAT_FEE"
    )
    if price:
        pid = _id(price, "price", "kaiten_get_prices")
        match = next((p for p in fees if p.id == pid), None)
    else:
        defaults = [p for p in fees if p.is_default]
        if period:
            match = next((p for p in defaults if p.billing_period == period), None)
        else:
            match = defaults[0] if len(defaults) == 1 else None
    if match is None:
        offered = [
            f"{p.id} ({p.billing_period}{', default' if p.is_default else ''})" for p in fees
        ]
        raise ToolError(
            f"License {license_slug} has no active flat fee matching that. Its flat fees: "
            f"{', '.join(offered) or 'none: add one with kaiten_save_prices'}. Pass billing_period or price."
        )
    return match


def register(mcp: FastMCP, *, writes: bool) -> None:
    @mcp.tool(annotations=READ)
    @_explained
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
        billing = sdk.kaiten().billing
        if not instance:
            caps = _capabilities(await billing.get_capabilities())
            if not caps["enabled"]:
                return {"billing": caps}
            settings, addons, vouchers = await asyncio.gather(
                billing.get_settings(),
                _part(
                    billing.addons.list_families(),
                    lambda families: [_family(f) for f in families],
                ),
                _part(billing.vouchers.list(), lambda rows: [_voucher(v) for v in rows]),
            )
            return {
                "billing": {
                    **caps,
                    "default_collection_method": settings.default_collection_method,
                    "default_days_until_due": settings.default_days_until_due,
                },
                "addons": addons,
                "vouchers": vouchers,
            }

        inst = await views.instance(instance)
        sub = await _subscription_of(inst.slug)

        async def upcoming_invoice() -> InvoicePreview | None:
            live = sub is not None and sub.status in LIVE
            return await billing.subscriptions.get_upcoming_invoice(inst.slug) if live else None

        upcoming, held, redeemed, invoices = await asyncio.gather(
            upcoming_invoice(),
            billing.instance_addons.list(inst.slug),
            _part(
                billing.instance_vouchers.list(inst.slug),
                lambda rows: [_redemption(r) for r in rows],
            ),
            billing.subscriptions.list_invoices(inst.slug, limit=5),
        )
        return shapes.drop_none(
            {
                "instance": inst.slug,
                "customer": inst.customer_slug,
                "license": inst.license_slug,
                "subscription": _subscription(sub) if sub else None,
                "upcoming_invoice": _composed(upcoming) if upcoming else None,
                "addons": [_held(a) for a in held],
                "vouchers": redeemed,
                "latest_invoices": [_invoice(i) for i in invoices],
                "notes": ["never subscribed: kaiten_subscribe_instance starts billing it"]
                if sub is None
                else None,
            }
        )

    @mcp.tool(annotations=READ)
    @_explained
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
        prices = sdk.kaiten().billing.license_prices
        lic = await views.resolve_license(plan=plan, license=license)
        listed = await prices.list(lic.slug, status=None if include_deprecated else "ACTIVE")
        out: dict[str, Any] = {
            "license": lic.slug,
            "state": lic.lifecycle_state,
            "prices": [_price(p) for p in listed],
        }
        scenario = (sample_usage, instance, add_ons, voucher_code, base_price)
        if preview or any(arg is not None for arg in scenario):
            if sample_usage and instance:
                raise ToolError("Pass sample_usage or instance, not both.")
            out["preview"] = _composed(
                await prices.preview_invoice(
                    lic.slug,
                    base_price_id=_id(base_price, "base_price", "kaiten_get_prices")
                    if base_price
                    else None,
                    sample_usage=_keyed(
                        {e: _decimal(q) for e, q in (sample_usage or {}).items()}, "entitlement"
                    ),
                    instance_slug=shapes.slug(instance, "instance") if instance else None,
                    add_ons=_keyed(add_ons, "add-on"),
                    voucher_code=voucher_code,
                )
            )
        return out

    @mcp.tool(annotations=READ)
    @_explained
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
        rows = await sdk.kaiten().billing.invoices.list(
            overdue=view == "overdue" or None,
            held=view == "held" or None,
            handoff_status={"handoff": "PENDING", "acknowledged": "ACKNOWLEDGED"}.get(view),
            customer_slug=shapes.slug(customer, "customer") if customer else None,
            instance_slug=shapes.slug(instance, "instance") if instance else None,
            status=status,
            limit=limit + 1,  # one past the page, to tell whether there are more
        )
        return shapes.drop_none(
            {
                "view": view,
                "invoices": [_invoice(i) for i in rows[:limit]],
                "more": len(rows) > limit or None,
            }
        )

    if not writes:
        return

    @mcp.tool(annotations=WRITE)
    @_explained
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
        prices = sdk.kaiten().billing.license_prices
        lic = await views.resolve_license(plan=None, license=license)
        by_id = {p.id: p for p in await prices.list(lic.slug)}
        done: dict[str, list] = {"deprecated": [], "added": []}
        unchanged: list[str] = []

        def same(p: Price, n: NewPrice) -> bool:
            return (
                p.status == "ACTIVE"
                and p.billing_model == n.billing_model
                and p.billing_period == n.billing_period
                and p.currency == n.currency
                and Decimal(p.unit_amount_decimal) == n.unit_amount
                and (p.metered.entitlement_slug if p.metered else None) == n.metered_entitlement
                and n.billing_timing in (None, p.billing_timing)
            )

        retire = [_id(raw, "price", "kaiten_get_prices") for raw in deprecate or []]
        missing = [pid for pid in retire if pid not in by_id]
        if missing:
            raise ToolError(f"License {lic.slug} has no price {', '.join(missing)}.")
        try:
            for pid in retire:
                if by_id[pid].status == "DEPRECATED":
                    unchanged.append(f"{pid} was already deprecated")
                    continue
                retired = await prices.deprecate(lic.slug, pid)
                by_id[pid] = retired  # no longer counts as an active twin of a new price
                done["deprecated"].append(_price(retired))
            for n in add or []:
                if any(same(p, n) for p in by_id.values()):
                    unchanged.append(f"an identical {n.billing_model} price is already active")
                    continue
                created = await prices.create(
                    lic.slug,
                    billing_model=n.billing_model,
                    unit_amount_decimal=_decimal(n.unit_amount),
                    currency=n.currency,
                    billing_period=n.billing_period,
                    billing_timing=n.billing_timing,
                    metered_entitlement_slug=shapes.slug(n.metered_entitlement, "entitlement")
                    if n.metered_entitlement
                    else None,
                    display_label=n.label,
                    is_default=n.is_default,
                )
                by_id[created.id] = created
                done["added"].append(_price(created))
        except APIStatusError as e:
            # Report what was written before the refusal, rather than an error that hides it.
            failed = sdk.describe(e)
        else:
            failed = None
        changes = {k: v for k, v in done.items() if v}
        after = [_price(p) for p in await prices.list(lic.slug, status="ACTIVE")]
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

    @mcp.tool(annotations=WRITE)
    @_explained
    async def kaiten_save_plan_settings(
        plan: Annotated[str, Field(description="A plan (license family slug)")],
        public: Annotated[
            bool | None,
            Field(
                description="List the plan in the organization's public catalogue, which its website reads "
                "with a publishable key: the plan's default version, once PUBLISHED, with its ACTIVE prices. "
                "A plan is private until made public"
            ),
        ] = None,
        fallback_plan: Annotated[
            str | None,
            Field(
                description="A FREE plan, every version of it priced FREE, whose default version an instance "
                "moves to when its subscription ends; '' for none"
            ),
        ] = None,
        trial_end_without_payment_method: Annotated[
            TrialEndWithoutPaymentMethod | None,
            Field(
                description="What a trial charged automatically becomes at its end when the customer has no "
                "active payment method: CANCEL ends the subscription; CONVERT makes it ACTIVE, and its first "
                "charge then fails"
            ),
        ] = None,
        trial_reminder_days: Annotated[
            int | None,
            Field(
                ge=1,
                le=30,
                description="How many days before a trial's end trial_will_end fires, for the trials "
                "started after the change",
            ),
        ] = None,
    ) -> dict:
        """A plan's own settings, the same for every version of it: whether the organization's public catalogue
        lists it, the FREE plan its instances fall back to when a subscription ends, and how its trials end. How
        each version is sold (pricing, payment method, trial length) is the version's own: kaiten_save_license.
        Only what you pass changes; asking for what is already set reports 'unchanged'. Scopes: write:licenses."""
        families = sdk.kaiten().license_families
        pslug = shapes.slug(plan, "plan")
        # With its versions: a plan with nothing published yet is found too.
        cur = await sdk.require(families.get(pslug, include_versions=True), f"plan {plan!r}")
        asked = shapes.drop_none(
            {
                "is_public": public,
                # "" removes the fallback, and is no slug.
                "fallback_license_family_slug": shapes.slug(fallback_plan, "fallback_plan")
                if fallback_plan
                else fallback_plan,
                "trial_end_without_payment_method": trial_end_without_payment_method,
                "trial_reminder_days": trial_reminder_days,
            }
        )
        held = {
            "is_public": cur.is_public,
            "fallback_license_family_slug": cur.fallback_license_family_slug or "",
            "trial_end_without_payment_method": cur.trial_end_without_payment_method,
            "trial_reminder_days": cur.trial_reminder_days,
        }
        # Only what differs is sent: a fallback restated would be checked again, and could be refused.
        change = {key: value for key, value in asked.items() if value != held[key]}
        after = await families.update(pslug, **change) if change else cur
        settings = shapes.plan_settings(after)
        notes: list[str] = []
        if after.is_public:
            listed = next(
                (
                    v
                    for v in cur.versions or []
                    if v.is_default and v.lifecycle_state == "PUBLISHED"
                ),
                None,
            )
            if listed is None:
                notes.append(
                    "not listed yet: the public catalogue offers a plan's default version only, once it is "
                    "PUBLISHED; publish one and make it the default with kaiten_set_license_state"
                )
            else:
                settings["listed_version"] = listed.slug
                if listed.pricing_type == "CUSTOM":
                    notes.append(
                        f"{listed.slug} is CUSTOM: the public catalogue offers it on request, through its "
                        "self_serve_cta_url; pricing FREE or PAID (kaiten_save_license) sells it at checkout"
                    )
        return shapes.report(
            "updated" if change else "unchanged",
            "plan_settings",
            settings,
            changes=shapes.changes(shapes.plan_settings(cur), shapes.plan_settings(after)),
            notes=notes,
        )

    @mcp.tool(annotations={**WRITE, "idempotentHint": False})
    @_explained
    async def kaiten_subscribe_instance(
        instance: Annotated[
            str, Field(description="Instance slug; its License version must be published")
        ],
        billing_period: Annotated[
            BillingPeriod | None,
            Field(description="Bill the default flat fee of this period of the instance's version"),
        ] = None,
        price: Annotated[
            str | None,
            Field(
                description="Or this exact flat fee of its version: a price id from kaiten_get_prices"
            ),
        ] = None,
        provider: Annotated[
            BillingProviderKind,
            Field(
                description="Who collects the invoices. NOOP: no provider, the organization's own accounting "
                "system takes them from the handoff queue. STRIPE: must be connected"
            ),
        ] = "NOOP",
        collection_method: Annotated[
            CollectionMethod | None,
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
        if current and current.status in LIVE:
            if current.base_price.id == base.id:
                return shapes.report(
                    "unchanged", "subscription", _subscription(current), instance=inst.slug
                )
            raise ToolError(
                f"Instance {inst.slug} is already billed ({current.status}) on "
                f"{current.base_price.display_label or current.base_price.id}. A move to another "
                "price applies at its next boundary, from the console; or cancel it first with "
                "kaiten_cancel_subscription."
            )
        started = await sdk.kaiten().billing.subscriptions.subscribe(
            inst.slug,
            base_price_id=base.id,
            provider_kind=provider,
            collection_method=collection_method,
            days_until_due=days_until_due,
            trial_days=trial_days,
            start_at=shapes.rfc3339(shapes.parse_time(start_at, "start_at")) if start_at else None,
            add_ons=_keyed(add_ons, "add-on"),
            voucher_code=voucher_code,
        )
        activation = started.activation_invoice
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
    @_explained
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
        k = sdk.kaiten()
        inst = await views.instance(instance)
        cur = await _subscription_of(inst.slug)
        if cur is None:
            raise ToolError(
                f"Instance {inst.slug} was never subscribed: there is nothing to cancel."
            )
        if cur.status == "CANCELED" or (when == "period_end" and cur.cancel_at_period_end):
            return shapes.report(
                "unchanged", "subscription", _subscription(cur), instance=inst.slug
            )
        after = await k.billing.subscriptions.cancel(
            inst.slug, mode="IMMEDIATE" if when == "now" else "AT_PERIOD_END", reason=reason
        )
        notes = []
        if after.status == "CANCELED":
            moved = (await k.instances.get(inst.slug)).license_slug
            if moved != inst.license_slug:
                notes.append(f"the instance fell back from {inst.license_slug} to {moved}")
        else:
            notes.append(
                "if its License family names a fallback, the instance moves to it at the boundary"
            )
        final = after.final_invoice
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
    @_explained
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
        addons = sdk.kaiten().billing.instance_addons
        islug, aslug = shapes.slug(instance, "instance"), shapes.slug(addon, "add-on")
        held = await addons.list(islug)
        cur = next((a for a in held if aslug in (a.addon_slug, a.family_slug)), None)
        if cur is None and quantity == 0:
            return shapes.report(
                "unchanged", "addon", {"instance": islug, "addon": aslug, "quantity": 0}
            )
        if cur is None:
            created = await addons.attach(islug, aslug, quantity=quantity)
            return shapes.report("created", "addon", {"instance": islug, **_held(created)})
        if quantity == 0:
            await addons.detach(islug, cur.addon_slug)
            return shapes.report(
                "deleted",
                "addon",
                {"instance": islug, **_held(cur)},
                notes=["its entitlements dropped at once; nothing is refunded"],
            )
        if cur.quantity == quantity and cur.pending_quantity is None:
            return shapes.report("unchanged", "addon", {"instance": islug, **_held(cur)})
        after = await addons.set_quantity(islug, cur.addon_slug, quantity)
        return shapes.report(
            "updated",
            "addon",
            {"instance": islug, **_held(after)},
            changes=shapes.changes(
                {"quantity": cur.quantity, "pending_quantity": cur.pending_quantity},
                {"quantity": after.quantity, "pending_quantity": after.pending_quantity},
            ),
        )

    @mcp.tool(annotations=WRITE)
    @_explained
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
            VoucherDuration,
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
        cur = await sdk.get_or_none(k.billing.vouchers.lookup(code))
        if cur is not None:
            if not (publish and cur.status == "DRAFT"):
                return shapes.report("unchanged", "voucher", _voucher(cur))
            after = await k.billing.vouchers.publish(cur.id)
            return shapes.report(
                "updated",
                "voucher",
                _voucher(after),
                changes={"status": {"from": "DRAFT", "to": after.status}},
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
        terms: dict[str, Any] = {}
        if discount:
            percent = discount.percent is not None
            terms = {
                "price_discount_type": "PERCENTAGE" if percent else "FIXED_AMOUNT",
                "price_discount_value": _decimal(discount.percent if percent else discount.amount),
                "currency": discount.currency,
                "price_applies_to": discount.applies_to,
            }
        created = await k.billing.vouchers.create(
            code=code,
            name=name,
            description=description,
            voucher_type=parts[0] if len(parts) == 1 else "COMPOSITE",
            duration=duration,
            duration_in_periods=periods,
            max_redemptions=max_redemptions,
            expires_at=shapes.rfc3339(shapes.parse_time(expires_at, "expires_at"))
            if expires_at
            else None,
            restricted_customer_slug=shapes.slug(customer, "customer") if customer else None,
            applicable_license_ids=[
                (await sdk.require(k.licenses.get(shapes.slug(s, "license")), f"license {s!r}")).id
                for s in licenses or []
            ]
            or None,
            **terms,
            grants=[
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
                for e, b in (boosts or {}).items()
            ]
            or None,
            flag_grants=_keyed(flag_grants, "flag"),
        )
        if publish:
            created = await k.billing.vouchers.publish(created.id)
        return shapes.report("created", "voucher", {"code": code, **_voucher(created)})

    @mcp.tool(annotations=WRITE)
    @_explained
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
            r = await sdk.kaiten().billing.instance_vouchers.redeem(islug, code)
        except (ConflictError, UnprocessableEntityError, NotFoundError) as e:
            if e.code == "RedeemVoucher.InstanceNotFound":
                raise
            status = "unchanged" if (e.code or "").endswith("AlreadyRedeemed") else "rejected"
            return shapes.report(
                status, "redemption", {"instance": islug}, reason=f"{e.code}: {e.detail}"
            )
        return shapes.report("created", "redemption", {"instance": islug, **_redemption(r)})

    @mcp.tool(annotations=WRITE)
    @_explained
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
        if cur.status == "PAID" and external_reference in (None, cur.handoff.external_reference):
            return shapes.report("unchanged", "invoice", _invoice(cur))
        after = await sdk.kaiten().billing.invoices.mark_paid(
            cur.id,
            external_reference=external_reference,
            paid_at=shapes.rfc3339(shapes.parse_time(paid_at, "paid_at")) if paid_at else None,
            note=note,
        )
        return shapes.report(
            "updated",
            "invoice",
            _invoice(after),
            changes=shapes.changes(_invoice(cur), _invoice(after)),
        )
