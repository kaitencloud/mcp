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
from typing import Annotated, Any, get_args
from urllib.parse import urlparse

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from kaitencloud import APIStatusError
from kaitencloud.webhooks import (
    FeatureFlagEvaluatedEvent,
    InstanceEntitlementValueGetEvent,
    WebhookEvent,
)
from pydantic import Field

from .. import sdk, shapes
from . import DESTRUCTIVE, READ, WRITE


# Every event Kaiten publishes: its type, which a subscription names; its name, which the audit trail
# shows; and what fires it. The SDK is the source: its webhook models are generated from Kaiten's
# OpenAPI document, so an SDK release brings new or renamed events here with no change to this module.
def _literal(model: Any, field: str) -> str:
    """The one value a generated event model's Literal field allows."""
    (value,) = get_args(model.model_fields[field].annotation)
    return value


def _sdk_events() -> dict[str, tuple[str, str]]:
    """Each event the SDK models, with its name and what fires it, read from its generated model."""
    union, _ = get_args(WebhookEvent)
    events = {}
    for model in get_args(union):
        type_, name = _literal(model, "type"), _literal(model, "name")
        doc = " ".join((model.__doc__ or "").split())
        fires = doc.split(". Delivered as", 1)[0].removeprefix("Triggered ").rstrip(".")
        events[type_] = (name, fires)
    return events


EVENTS = _sdk_events()
BY_NAME = {name: type_ for type_, (name, _) in EVENTS.items()}
# One delivery per flag evaluation and per usage read: the console keeps them out of its dialog.
EVERY_CALL = {
    _literal(m, "type") for m in (FeatureFlagEvaluatedEvent, InstanceEntitlementValueGetEvent)
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
            "the catalogue of the kaitencloud SDK this server runs; the console lists events newer than it."
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
