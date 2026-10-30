# Copyright 2026 KAITEN INC
# SPDX-License-Identifier: Apache-2.0

"""Webhooks: Kaiten Cloud's deliveries of Kaiten's events to an HTTPS endpoint.

Kaiten Cloud serves the /webhooks routes, to an organization whose own Kaiten License grants
webhook events; a self-hosted Kaiten serves none, and publishes the same events to its event
stream instead. An endpoint receives the event types it names -- there is no "every event" --
and its signing secret is what the receiver verifies each delivery with.

A webhook URL can be a credential too: a Slack incoming-webhook URL is one. Answers show its scheme
and host, never its path or query; endpoints are told apart by their id.

The routes are in no OpenAPI document, so in no SDK: they go through the SDK's transport as
plain JSON, in the shapes Kaiten's console sends.
"""

from __future__ import annotations

import difflib
from collections import Counter
from typing import Annotated, Any
from urllib.parse import urlparse

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from kaitencloud import APIStatusError
from pydantic import Field

from .. import sdk, shapes
from . import DESTRUCTIVE, READ, WRITE

# Every event Kaiten publishes: its type, which a subscription names; its name, which the audit
# trail shows; and what fires it. The `webhooks` section of Kaiten 26.10.7's OpenAPI document,
# which the console's catalogue is checked against; sdk-python 1.0 models 55 of them.
CATALOGUE = """\
com.kaiten.addon.entitlement.v1.assigned ADDON_ENTITLEMENT_ASSIGNED when an add-on version grants an entitlement
com.kaiten.addon.entitlement.v1.unassigned ADDON_ENTITLEMENT_UNASSIGNED when an add-on grant is removed
com.kaiten.addon.entitlement.v1.updated ADDON_ENTITLEMENT_UPDATED when an add-on grant changes
com.kaiten.addon.price.v1.created ADDON_PRICE_CREATED when a price is added to an add-on version
com.kaiten.addon.price.v1.deprecated ADDON_PRICE_DEPRECATED when an add-on price stops being offered
com.kaiten.addon.v1.archived ADDON_ARCHIVED when an add-on version is withdrawn from sale
com.kaiten.addon.v1.created ADDON_CREATED when an add-on version is created
com.kaiten.addon.v1.deleted ADDON_DELETED when an add-on version is deleted
com.kaiten.addon.v1.published ADDON_PUBLISHED when a draft add-on version goes on sale
com.kaiten.addon.v1.unarchived ADDON_UNARCHIVED when an archived add-on version goes back on sale
com.kaiten.addon.v1.updated ADDON_UPDATED when an add-on version, its compatibility or its family's visibility changes
com.kaiten.billing_provider.v1.connected BILLING_PROVIDER_CONNECTED when an organization connects a payment provider (activates its connector)
com.kaiten.billing_provider.v1.disconnected BILLING_PROVIDER_DISCONNECTED when an organization disconnects a payment provider (deactivates its connector)
com.kaiten.billing_provider.v1.sync_failed BILLING_PROVIDER_SYNC_FAILED when syncing with a payment provider has failed three passes in a row, once per streak
com.kaiten.checkout_session.v1.abandoned CHECKOUT_SESSION_ABANDONED when an anonymous checkout ends unpaid: still OPEN 24 h after its start (status ABANDONED), or its INCOMPLETE subscription expired or was canceled (status EXPIRED, with the customer and instance to clean up)
com.kaiten.checkout_session.v1.completed CHECKOUT_SESSION_COMPLETED when an anonymous checkout completes: its ACTIVATION invoice is paid, or nothing was due
com.kaiten.checkout_session.v1.started CHECKOUT_SESSION_STARTED when an anonymous checkout starts (POST /public/checkout-sessions)
com.kaiten.component.v1.created COMPONENT_CREATED when a new component is created
com.kaiten.component.v1.deleted COMPONENT_DELETED when a component is deleted
com.kaiten.component.v1.updated COMPONENT_UPDATED when a component is updated or auto-versioned
com.kaiten.customer.payment_method.v1.attached CUSTOMER_PAYMENT_METHOD_ATTACHED when a customer's default payment method in a provider is set or replaced (a completed setup session, or a change in the provider's portal)
com.kaiten.customer.payment_method.v1.detached CUSTOMER_PAYMENT_METHOD_DETACHED when a customer no longer has a payment method in a provider
com.kaiten.customer.payment_method.v1.expiring CUSTOMER_PAYMENT_METHOD_EXPIRING once, 30 days before the end of the expiry month of a customer's payment method
com.kaiten.customer.v1.created CUSTOMER_CREATED when a new customer is created
com.kaiten.customer.v1.creation_rejected CUSTOMER_CREATION_REJECTED when a customer creation is refused because the organization has reached its customer entitlement cap
com.kaiten.customer.v1.deleted CUSTOMER_DELETED when a customer is deleted
com.kaiten.customer.v1.updated CUSTOMER_UPDATED when a customer is updated
com.kaiten.deployment_zone.v1.created DEPLOYMENT_ZONE_CREATED when a new deployment zone is created
com.kaiten.deployment_zone.v1.deleted DEPLOYMENT_ZONE_DELETED when a deployment zone is deleted
com.kaiten.deployment_zone.v1.release_deployed RELEASE_DEPLOYED when a release is deployed to a deployment zone, either at zone creation or when an existing zone's release changes
com.kaiten.deployment_zone.v1.updated DEPLOYMENT_ZONE_UPDATED when a deployment zone is updated
com.kaiten.entitlement.v1.created ENTITLEMENT_CREATED when a new entitlement is created
com.kaiten.entitlement.v1.deleted ENTITLEMENT_DELETED when an entitlement is deleted
com.kaiten.entitlement.v1.updated ENTITLEMENT_UPDATED when an entitlement is updated
com.kaiten.entitlement_group.v1.created ENTITLEMENT_GROUP_CREATED when a new entitlement group is created
com.kaiten.entitlement_group.v1.deleted ENTITLEMENT_GROUP_DELETED when an entitlement group is deleted
com.kaiten.entitlement_group.v1.updated ENTITLEMENT_GROUP_UPDATED when an entitlement group is updated
com.kaiten.feature_flag.v1.created FEATURE_FLAG_CREATED when a new feature flag is created
com.kaiten.feature_flag.v1.deleted FEATURE_FLAG_DELETED when a feature flag is deleted
com.kaiten.feature_flag.v1.evaluated FEATURE_FLAG_EVALUATED on every flag evaluation
com.kaiten.feature_flag.v1.updated FEATURE_FLAG_UPDATED when a feature flag is updated
com.kaiten.identity.v1.system_token_issued SYSTEM_ORGANIZATION_TOKEN_ISSUED when a platform credential mints an organization-scoped credential for the `system:kaiten` platform identity inside this organization
com.kaiten.instance.addon.v1.added INSTANCE_ADDON_ADDED when an add-on is attached to an instance
com.kaiten.instance.addon.v1.quantity_changed INSTANCE_ADDON_QUANTITY_CHANGED when the quantity of an instance's add-on changes
com.kaiten.instance.addon.v1.removed INSTANCE_ADDON_REMOVED when an add-on is removed from an instance
com.kaiten.instance.billing.v1.canceled INSTANCE_BILLING_CANCELED when a subscription is canceled: at its period's end, immediately, or during its trial
com.kaiten.instance.billing.v1.cancellation_reverted INSTANCE_BILLING_CANCELLATION_REVERTED when a scheduled cancellation is reverted before the boundary
com.kaiten.instance.billing.v1.cancellation_scheduled INSTANCE_BILLING_CANCELLATION_SCHEDULED when a subscription is set to cancel at the end of its period
com.kaiten.instance.billing.v1.plan_change_canceled INSTANCE_BILLING_PLAN_CHANGE_CANCELED when a scheduled plan change is dropped: by request, or by a cancellation
com.kaiten.instance.billing.v1.plan_change_scheduled INSTANCE_BILLING_PLAN_CHANGE_SCHEDULED when a plan change is scheduled for the next boundary, or replaced
com.kaiten.instance.billing.v1.plan_changed INSTANCE_BILLING_PLAN_CHANGED when a scheduled plan change is applied at a boundary
com.kaiten.instance.billing.v1.provider_changed INSTANCE_BILLING_PROVIDER_CHANGED when a subscription moves to another payment provider, from its next invoice on
com.kaiten.instance.billing.v1.started INSTANCE_BILLING_STARTED when an instance is subscribed, or a CANCELED subscription subscribed again
com.kaiten.instance.billing.v1.status_changed INSTANCE_BILLING_STATUS_CHANGED when a trial converts, or a subscription becomes PAST_DUE or leaves it
com.kaiten.instance.billing.v1.trial_will_end INSTANCE_BILLING_TRIAL_WILL_END once per trial, at reminderAt: its family's trialReminderDays before trialEndsAt (3 by default), or at once when the trial is shorter
com.kaiten.instance.entitlement.v1.cap_exceeded INSTANCE_ENTITLEMENT_CAP_EXCEEDED when a SOFT-enforcement entitlement's usage crosses from below the cap to above it
com.kaiten.instance.entitlement.v1.usage_period_rolled_over INSTANCE_ENTITLEMENT_USAGE_PERIOD_ROLLED_OVER once per usage window closed by a report: the entitlement's own stored bucket, and one synthetic event per window skipped entirely since the last report
com.kaiten.instance.entitlement.v1.usage_reached INSTANCE_ENTITLEMENT_USAGE_REACHED when an instance's entitlement usage reaches its threshold
com.kaiten.instance.entitlement.v1.usage_report_accepted ENTITLEMENT_USAGE_REPORT_ACCEPTED when a usage report is persisted, including a SOFT-enforcement overage
com.kaiten.instance.entitlement.v1.usage_report_rejected ENTITLEMENT_USAGE_REPORT_REJECTED when a usage report is refused because it would take a HARD-enforcement entitlement past its cap
com.kaiten.instance.entitlement.v1.usage_warning_threshold_reached INSTANCE_ENTITLEMENT_USAGE_WARNING_THRESHOLD_REACHED when an instance's entitlement usage crosses its configured early-warning percentage boundary, before the cap is reached
com.kaiten.instance.entitlement.v1.value_get ENTITLEMENT_VALUE_GET on every read of an instance entitlement's usage value
com.kaiten.instance.entitlement_exception.v1.created INSTANCE_ENTITLEMENT_EXCEPTION_CREATED when an exception is created on one entitlement of an instance
com.kaiten.instance.entitlement_exception.v1.expired INSTANCE_ENTITLEMENT_EXCEPTION_EXPIRED once per exception, when its endsAt passes; never for a revoked one
com.kaiten.instance.entitlement_exception.v1.revoked INSTANCE_ENTITLEMENT_EXCEPTION_REVOKED when an exception is revoked
com.kaiten.instance.invoice.v1.handoff_acknowledged INSTANCE_INVOICE_HANDOFF_ACKNOWLEDGED when the organization's accounting system acknowledges an invoice from the handoff queue, or the invoice is marked paid while still waiting there
com.kaiten.instance.invoice.v1.held INSTANCE_INVOICE_HELD when a period closes into an invoice held as a DRAFT, because the usage journal it was measured from failed a consistency check
com.kaiten.instance.invoice.v1.issued INSTANCE_INVOICE_ISSUED once per invoice with something owed, when it is issued
com.kaiten.instance.invoice.v1.marked_uncollectible INSTANCE_INVOICE_MARKED_UNCOLLECTIBLE when an invoice is written off
com.kaiten.instance.invoice.v1.paid INSTANCE_INVOICE_PAID when an invoice is paid: marked paid by the organization, or issued paid because nothing was owed
com.kaiten.instance.invoice.v1.payment_failed INSTANCE_INVOICE_PAYMENT_FAILED when the automatic charge of an invoice is refused, or needs the customer to authenticate it on the invoice's hosted page
com.kaiten.instance.invoice.v1.push_failed INSTANCE_INVOICE_PUSH_FAILED when pushing an invoice to its payment provider has failed as many times as the alert threshold, then once a day while it keeps failing
com.kaiten.instance.invoice.v1.pushed INSTANCE_INVOICE_PUSHED when an invoice's payment provider has issued it, right after INSTANCE_INVOICE_ISSUED
com.kaiten.instance.invoice.v1.reconciliation_mismatch INSTANCE_INVOICE_RECONCILIATION_MISMATCH when an invoice's payment provider holds amounts that differ from Kaiten's
com.kaiten.instance.invoice.v1.released INSTANCE_INVOICE_RELEASED when a held invoice leaves its hold: released by the organization, recomposed from a sound journal, or found sound by a later check
com.kaiten.instance.invoice.v1.voided INSTANCE_INVOICE_VOIDED when an invoice is voided
com.kaiten.instance.v1.created INSTANCE_CREATED when a new instance is created
com.kaiten.instance.v1.deleted INSTANCE_DELETED when an instance is deleted
com.kaiten.instance.v1.deployed INSTANCE_DEPLOYED when an instance is deployed to a deployment zone
com.kaiten.instance.v1.lifecycle_stage_changed INSTANCE_LIFECYCLE_STAGE_CHANGED when an instance commercial lifecycle stage changes
com.kaiten.instance.v1.migrated INSTANCE_MIGRATED when an instance is migrated from one deployment zone to another
com.kaiten.instance.v1.status_changed INSTANCE_STATUS_CHANGED when an instance operational status changes
com.kaiten.instance.v1.updated INSTANCE_UPDATED when an instance is updated, by a write or by billing: the licence version a paid checkout bought, or the fallback a subscription that ended moves it to (licenseChangeReason BILLING_FALLBACK)
com.kaiten.instance.voucher.v1.expired INSTANCE_VOUCHER_EXPIRED when a PRICE redemption has discounted its last invoice, or a boost's window ends
com.kaiten.instance.voucher.v1.redeemed INSTANCE_VOUCHER_REDEEMED when an instance redeems a voucher
com.kaiten.instance.voucher.v1.revoked INSTANCE_VOUCHER_REVOKED when a redemption is revoked
com.kaiten.license.entitlement.v1.assigned LICENSE_ENTITLEMENT_ASSIGNED when an entitlement is assigned to a license
com.kaiten.license.entitlement.v1.unassigned LICENSE_ENTITLEMENT_UNASSIGNED when an entitlement is unassigned from a license
com.kaiten.license.entitlement.v1.updated LICENSE_ENTITLEMENT_UPDATED when a license entitlement is updated
com.kaiten.license.price.v1.created LICENSE_PRICE_CREATED when a price is added to a licence version
com.kaiten.license.price.v1.deprecated LICENSE_PRICE_DEPRECATED when a licence price is deprecated
com.kaiten.license.price.v1.updated LICENSE_PRICE_UPDATED when a price of a DRAFT licence version is edited
com.kaiten.license.v1.archived LICENSE_ARCHIVED when a published license version is archived and withdrawn from sale
com.kaiten.license.v1.created LICENSE_CREATED when a new license is created
com.kaiten.license.v1.deleted LICENSE_DELETED when a license is deleted
com.kaiten.license.v1.published LICENSE_PUBLISHED when a draft license version is published and goes on sale
com.kaiten.license.v1.unarchived LICENSE_UNARCHIVED when an archived license version is put back on sale
com.kaiten.license.v1.updated LICENSE_UPDATED when a license is updated
com.kaiten.license_family.v1.created LICENSE_FAMILY_CREATED when a license is created without naming a family: its version 1 opens a new family, which this event carries, with the version it serves -- none if that version is a draft
com.kaiten.license_family.v1.deleted LICENSE_FAMILY_DELETED when a license family's last version is deleted, which deletes the family and frees its slug
com.kaiten.license_family.v1.updated LICENSE_FAMILY_UPDATED when a license family starts serving another version, or none: a newer version published, a version published, archived or unarchived, the family's default taken or given up, or the served version deleted
com.kaiten.metadata_field.v1.archived METADATA_FIELD_ARCHIVED when a metadata field is archived and leaves the active schema composition
com.kaiten.metadata_field.v1.created METADATA_FIELD_CREATED when a metadata field is declared for a resource type
com.kaiten.metadata_field.v1.reordered METADATA_FIELD_REORDERED when the display order of a resource type's metadata fields is rewritten
com.kaiten.metadata_field.v1.unarchived METADATA_FIELD_UNARCHIVED when an archived metadata field is restored to the active schema composition
com.kaiten.metadata_field.v1.updated METADATA_FIELD_UPDATED when a metadata field's label or JSON schema changes
com.kaiten.publishable_key.v1.created PUBLISHABLE_KEY_CREATED when a publishable key is issued
com.kaiten.publishable_key.v1.revoked PUBLISHABLE_KEY_REVOKED once, when a publishable key is revoked
com.kaiten.release.v1.created RELEASE_CREATED when a new release is created
com.kaiten.release.v1.deleted RELEASE_DELETED when a release is deleted
com.kaiten.voucher.v1.archived VOUCHER_ARCHIVED when a voucher is archived
com.kaiten.voucher.v1.created VOUCHER_CREATED when a voucher is created
com.kaiten.voucher.v1.exhausted VOUCHER_EXHAUSTED by the redemption that reaches maxRedemptions
com.kaiten.voucher.v1.expired VOUCHER_EXPIRED when an active voucher reaches its expiresAt and can no longer be redeemed
com.kaiten.voucher.v1.published VOUCHER_PUBLISHED when a draft voucher becomes redeemable
com.kaiten.voucher.v1.updated VOUCHER_UPDATED when a voucher changes
"""
EVENTS = {
    type_: (name, fires)
    for type_, name, fires in (line.split(" ", 2) for line in CATALOGUE.splitlines())
}
BY_NAME = {name: type_ for type_, (name, _) in EVENTS.items()}
# One delivery per flag evaluation and per usage read: the console keeps them out of its dialog.
EVERY_CALL = {
    "com.kaiten.feature_flag.v1.evaluated",
    "com.kaiten.instance.entitlement.v1.value_get",
}

CONSUMING_EVENTS = "https://docs.kaiten.sh/docs/self-hosting/consuming-events"


def find_events(query: str) -> list[dict]:
    """The events whose type, name or trigger hold every word of the query, else any of them."""

    def words(text: str) -> str:
        return text.lower().replace("_", " ").replace(".", " ")

    wanted = words(query).split()
    text = {t: words(f"{t} {name} {fires}") for t, (name, fires) in EVENTS.items()}
    hits = [t for t in EVENTS if all(w in text[t] for w in wanted)] or [
        t for t in EVENTS if any(w in text[t] for w in wanted)
    ]
    return [{"type": t, "name": EVENTS[t][0], "fires": EVENTS[t][1]} for t in hits]


def as_event_types(given: list[str]) -> tuple[list[str], list[str]]:
    """The types a subscription filters on, from types or names, and notes on the costly ones.

    The API takes any well-formed type, and one it never publishes would silently match nothing:
    such a type is refused here instead, with the closest ones.
    """
    types, unknown = [], []
    for raw in given:
        value = raw.strip()
        type_ = value if value in EVENTS else BY_NAME.get(value.upper())
        if type_ is None:
            unknown.append(value)
        elif type_ not in types:
            types.append(type_)
    if unknown:
        hints = []
        for value in unknown:
            close = difflib.get_close_matches(value, [*EVENTS, *BY_NAME], n=3, cutoff=0.6)
            hints.append(f"{value!r}" + (f" (closest: {', '.join(close)})" if close else ""))
        raise ToolError(
            f"Not an event Kaiten publishes: {'; '.join(hints)}. kaiten_get_webhooks(events=...) searches "
            "the catalogue of Kaiten 26.10.7; the console lists events newer than this server."
        )
    notes = [
        f"{EVENTS[t][0]} fires {EVENTS[t][1]}: one delivery each time, counted against the "
        "organization's monthly allowance of webhook events"
        for t in types
        if t in EVERY_CALL
    ]
    return types, notes


async def _call(method: str, path: str, body: Any = None) -> Any:
    """A webhooks route. Where Kaiten does not serve webhooks, its answer becomes an explanation,
    raised as the tool's own error."""
    try:
        return await sdk.call_unwrapped(method, path, body)
    except APIStatusError as e:
        if e.status_code == 404 and not (e.code or "").startswith("Svix."):
            raise ToolError(
                "This Kaiten serves no webhooks: they are part of Kaiten Cloud. A self-hosted Kaiten "
                "publishes the same events, with the same types, to its event stream, the kaiten.events "
                f"exchange of its RabbitMQ: attach a consumer there ({CONSUMING_EVENTS}). "
                "kaiten_get_webhooks(events=...) finds the types to consume."
            ) from e
        if e.code == "Webhooks.NotEntitled":
            raise ToolError(
                "This organization's Kaiten Cloud License grants it no webhook events (its webhooks "
                "entitlement), so Kaiten serves it no webhooks."
            ) from e
        if e.status_code == 503:
            raise ToolError(
                "Kaiten Cloud could not check whether this organization's License includes webhooks, "
                "and changed nothing: try again."
            ) from e
        raise


def masked(url: str) -> str:
    """A webhook URL as answers show it: its scheme and host, with any port. Its path and query,
    and credentials before the host, can be the secret itself, so they are elided."""
    parts = urlparse(url)
    return f"{parts.scheme}://{parts.netloc.rpartition('@')[2]}/…"


def _scrubbed(text: str | None, url: str) -> str | None:
    """A receiver's answer, without the endpoint's path and query: an error page can echo them."""
    parts = urlparse(url)
    for secret in (url, parts.path, parts.query):
        if text and len(secret) > 1:
            text = text.replace(secret, "…")
    return text


def _hook(h: dict) -> dict:
    # Never the signing secret: it is shown once, by kaiten_save_webhook when it creates one.
    return shapes.drop_none(
        {
            "id": h["id"],
            "url": masked(h["url"]),
            "event_types": sorted(h.get("eventTypes") or []),
            "created_at": h.get("createdAt"),
        }
    )


def _delivery(d: dict, url: str) -> dict:
    failed = d["status"] == "fail"
    return shapes.drop_none(
        {
            "at": d.get("date"),
            "event": d.get("eventType") or None,
            "status": d["status"],
            "response": d.get("responseStatusCode"),
            "response_text": _scrubbed(d.get("responseStatusText"), url) if failed else None,
        }
    )


async def _webhook(webhook: str) -> dict:
    """An endpoint, by its id or its full URL."""
    hooks = await _call("GET", "webhooks") or []
    match = next((h for h in hooks if webhook.strip() in (h["id"], h["url"])), None)
    if match is None:
        shown = masked(webhook) if "://" in webhook else webhook
        raise ToolError(f"No webhook {shown!r}: kaiten_get_webhooks lists them.")
    return match


def register(mcp: FastMCP, *, writes: bool) -> None:
    @mcp.tool(annotations=READ)
    async def kaiten_get_webhooks(
        events: Annotated[
            str | None,
            Field(
                description="Find event types instead: words to look for in the catalogue of the events "
                "Kaiten publishes -- their types, names, and what fires them -- e.g. 'warning threshold'. "
                "Answered from the catalogue alone, on any edition"
            ),
        ] = None,
        history: Annotated[
            int, Field(ge=0, le=50, description="Recent deliveries to show per endpoint")
        ] = 10,
    ) -> dict:
        """Kaiten Cloud's webhook endpoints: each one's id, its URL's scheme and host, the event types it receives, and
        its delivery history -- how many of its latest attempts succeeded and failed, and the most recent, newest
        first, with the endpoint's response. A URL's path and query are never shown, nor a signing secret: the path
        can be a credential, as in a Slack incoming-webhook URL. Endpoints on one host are told apart by their id,
        which kaiten_delete_webhook takes. With events: the event types that match, to subscribe to; a self-hosted
        Kaiten's event stream carries the same types. Scopes: read:webhooks."""
        if events is not None:
            matches = find_events(events)
            return shapes.drop_none(
                {
                    "total": len(matches),
                    "events": matches[:25],
                    "truncated": len(matches) > 25 or None,
                }
            )
        hooks = await _call("GET", "webhooks") or []
        past = (await _call("GET", "webhooks/history") or {}).get("history") or []
        past.sort(key=lambda d: d.get("date") or "", reverse=True)
        rows = []
        for h in hooks:
            mine = [d for d in past if d["hookId"] == h["id"]]
            rows.append(
                {
                    **_hook(h),
                    "deliveries": dict(Counter(d["status"] for d in mine)),
                    "recent": [_delivery(d, h["url"]) for d in mine[:history]],
                }
            )
        return {"webhooks": rows}

    if not writes:
        return

    @mcp.tool(annotations=WRITE)
    async def kaiten_save_webhook(
        url: Annotated[
            str,
            Field(
                description="The HTTPS endpoint that receives a POST for each event. A Slack incoming-webhook "
                "URL gets messages formatted for Slack"
            ),
        ],
        event_types: Annotated[
            list[str],
            Field(
                min_length=1,
                description="Every event it receives, by type (com.kaiten.instance.v1.created) or name "
                "(INSTANCE_CREATED): kaiten_get_webhooks(events=...) finds them. For an endpoint that "
                "exists, the complete list",
            ),
        ],
    ) -> dict:
        """Create a webhook endpoint on Kaiten Cloud, or set the events of the one that already delivers to this URL.
        A new endpoint's signing secret, which the receiver verifies every delivery with, is in the answer that
        creates it, and in no other answer of this server: store it then. The answer shows the URL's scheme and host
        only, and the endpoint's id. An event type Kaiten does not publish is refused, with the closest ones. Kaiten
        counts the events delivered, not the endpoints, against the organization's monthly allowance.
        Scopes: write:webhooks."""
        types, notes = as_event_types(event_types)
        target = urlparse(url.strip())
        if target.scheme not in ("http", "https") or not target.netloc:
            raise ToolError("url is not an absolute http(s) URL: it needs a scheme and a host.")
        hooks = await _call("GET", "webhooks") or []
        cur = next((h for h in hooks if h["url"] == url.strip()), None)
        if cur is None:
            created = await _call("POST", "webhooks", {"url": url.strip(), "eventTypes": types})
            # The create answer carries no secret: reading the endpoint is how the console shows it.
            try:
                secret = created.get("signingSecret") or (
                    await _call("GET", f"webhooks/{created['id']}")
                ).get("signingSecret")
            except APIStatusError:
                secret = None
            notes.insert(
                0,
                "store the signing secret now: no other answer of this server shows it"
                if secret
                else "the signing secret could not be read: the console shows it (Integrations > Webhooks)",
            )
            return shapes.report(
                "created",
                "webhook",
                {**_hook(created), "signing_secret": secret},
                notes=notes,
            )
        if sorted(cur.get("eventTypes") or []) == sorted(types):
            return shapes.report("unchanged", "webhook", _hook(cur), notes=notes)
        after = await _call(
            "PUT", f"webhooks/{cur['id']}", {"url": cur["url"], "eventTypes": types}
        )
        return shapes.report(
            "updated",
            "webhook",
            _hook(after),
            changes=shapes.changes(
                {"event_types": sorted(cur.get("eventTypes") or [])},
                {"event_types": _hook(after)["event_types"]},
            ),
            notes=notes,
        )

    @mcp.tool(annotations=DESTRUCTIVE)
    async def kaiten_delete_webhook(
        webhook: Annotated[
            str,
            Field(description="The endpoint's id, from kaiten_get_webhooks, or its full URL"),
        ],
        confirm: Annotated[
            bool,
            Field(description="false (default): only show what would stop. true: delete"),
        ] = False,
    ) -> dict:
        """Delete a webhook endpoint: deliveries to it stop at once. Call it first without confirm to see the endpoint
        and the events it receives, then with confirm=true. Its URL shows as its scheme and host only.
        Scopes: write:webhooks."""
        hook = await _webhook(webhook)
        if not confirm:
            return shapes.report(
                "preview",
                "webhook",
                _hook(hook),
                notes=["Nothing was deleted. Call again with confirm=true to delete."],
            )
        await _call("DELETE", f"webhooks/{hook['id']}")
        return shapes.report("deleted", "webhook", _hook(hook))
