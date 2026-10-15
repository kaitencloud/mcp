"""Machine access (service accounts and their ksh_ tokens) and the audit trail."""

from __future__ import annotations

from typing import Annotated, Any, get_args

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from kaitencloud.types import Scope
from pydantic import Field

from .. import sdk, shapes, views
from . import DESTRUCTIVE, READ, WRITE

# The scopes the API knows, as the SDK's contract lists them. The API also
# accepts the read:* / write:* wildcards, which the contract does not enumerate.
SCOPES = sorted(get_args(Scope))
SCOPES_HELP = (
    "Scopes to grant, least privilege: any of "
    + ", ".join(SCOPES)
    + "; or read:* / write:* for every module"
)


def _token(t: Any) -> dict:
    return shapes.drop_none(
        {
            "slug": t.slug,
            "name": t.name,
            "scopes": list(t.scopes or []),
            "expires_at": shapes.iso(t.expires_at),
            "created_at": shapes.iso(t.created_at),
            "created_by": t.created_by.name if t.created_by else None,
        }
    )


def register(mcp: FastMCP, *, writes: bool) -> None:
    @mcp.tool(annotations=READ)
    async def kaiten_get_access(
        service_account: Annotated[
            str | None, Field(description="Only this service account")
        ] = None,
    ) -> dict:
        """Machine access to the organization: each service account and its active tokens, with their scopes and
        expiry. Token values are never shown after they are issued."""
        k = sdk.kaiten()
        if service_account:
            sa = shapes.slug(service_account, "service_account")
            accounts = [await sdk.require(k.service_accounts.get(sa), f"service account {sa!r}")]
        else:
            accounts = await k.service_accounts.list()
        tokens = await views.gather_bounded(
            k.service_accounts.list_tokens(a.slug) for a in accounts
        )
        return {
            "service_accounts": [
                {
                    "slug": a.slug,
                    "name": a.name,
                    "tokens": [_token(t) for t in ts if not t.revoked_at],
                }
                for a, ts in zip(accounts, tokens, strict=True)
            ],
        }

    @mcp.tool(annotations=READ)
    async def kaiten_get_audit_trail(
        instance: Annotated[
            str | None,
            Field(description="Only this instance's events; every instance's when omitted"),
        ] = None,
        event: Annotated[
            str | None,
            Field(description="Only this event name, e.g. INSTANCE_ENTITLEMENT_CAP_EXCEEDED"),
        ] = None,
        since: Annotated[str | None, Field(description="ISO date or timestamp")] = None,
        until: Annotated[str | None, Field(description="ISO date or timestamp")] = None,
        limit: Annotated[int, Field(ge=1, le=200)] = 25,
        include_payload: Annotated[bool, Field(description="Include each event's payload")] = False,
    ) -> dict:
        """What happened, newest first: instance lifecycle (created, deployed, migrated, status changes), usage reports
        accepted and rejected, limits reached, warnings and overages, period rollovers."""
        k = sdk.kaiten()
        after = shapes.parse_time(since, "since") if since else None
        before = shapes.parse_time(until, "until") if until else None
        slugs = (
            [(await views.instance(instance)).slug]
            if instance
            else [i.slug for i in await k.instances.list()]
        )
        pages = await views.gather_bounded(
            k.instances.list_audit_trails(
                s, event_name=event, after=after, before=before, limit=limit
            )
            for s in slugs
        )
        events = sorted(
            (e for page in pages for e in page),
            key=lambda e: shapes.as_time(e.timestamp),
            reverse=True,
        )
        rows = []
        for e in events[:limit]:
            row = {
                "event": e.event_name,
                "instance": e.instance_slug,
                "at": shapes.iso(e.timestamp),
            }
            if include_payload:
                row["payload"] = e.payload
            rows.append(row)
        return shapes.drop_none({"events": rows, "more": len(events) > limit or None})

    if not writes:
        return

    @mcp.tool(annotations={**WRITE, "idempotentHint": False})
    async def kaiten_issue_token(
        service_account: Annotated[
            str, Field(description="Service account slug; created when it does not exist yet")
        ],
        token_name: Annotated[
            str, Field(description="What the token is for, e.g. 'production backend'")
        ],
        scopes: Annotated[list[str], Field(min_length=1, description=SCOPES_HELP)],
        expires_at: Annotated[
            str | None, Field(description="ISO date; the token never expires when omitted")
        ] = None,
        service_account_name: Annotated[
            str | None, Field(description="Display name, when the service account is created")
        ] = None,
    ) -> dict:
        """Issue a ksh_ API token for an integration (an SDK, a backend, CI), creating its service account if needed.
        The token value is in this answer and nowhere else: it cannot be read again."""
        k = sdk.kaiten()
        unknown = [s for s in scopes if s not in SCOPES and s not in ("read:*", "write:*")]
        if unknown:
            raise ToolError(f"Unknown scope(s) {unknown}. {SCOPES_HELP}.")
        sa = shapes.slug(service_account, "service_account")
        notes = []
        if await sdk.get_or_none(k.service_accounts.get(sa)) is None:
            await k.service_accounts.create(
                slug=sa, name=service_account_name or sa.replace("-", " ").title()
            )
            notes.append(f"created service account {sa}")
        t = await k.service_accounts.create_token(
            sa,
            name=token_name,
            scopes=scopes,
            expires_at=shapes.parse_time(expires_at, "expires_at") if expires_at else None,
        )
        notes.append("store the token now: it is shown only once")
        return shapes.report(
            "created", "token", {**_token(t), "service_account": sa, "token": t.token}, notes=notes
        )

    @mcp.tool(annotations=DESTRUCTIVE)
    async def kaiten_revoke_token(
        service_account: Annotated[str, Field(description="Service account slug")],
        token: Annotated[str, Field(description="Token slug, from kaiten_get_access")],
    ) -> dict:
        """Revoke a service account token. Every replica stops accepting it at once; whatever used it starts failing."""
        k = sdk.kaiten()
        sa = shapes.slug(service_account, "service_account")
        match = next((t for t in await k.service_accounts.list_tokens(sa) if t.slug == token), None)
        if match is None:
            raise ToolError(f"Service account {sa} has no token {token!r}.")
        if match.revoked_at:
            return shapes.report("unchanged", "token", _token(match), notes=["already revoked"])
        await k.service_accounts.delete_token(sa, token)
        return shapes.report("deleted", "token", _token(match))
