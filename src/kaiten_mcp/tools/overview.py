# Copyright 2026 KAITEN INC
# SPDX-License-Identifier: Apache-2.0

"""Orientation: what this organization holds, and finding things in it."""

from __future__ import annotations

import asyncio
from typing import Annotated, Any, Literal

from fastmcp import FastMCP
from pydantic import Field

from .. import ai_policy, sdk, shapes, views
from . import READ

Kind = Literal[
    "customer",
    "instance",
    "plan",
    "license",
    "entitlement",
    "entitlement_group",
    "feature_flag",
    "deployment_zone",
    "release",
    "component",
    "service_account",
]

# kind -> (SDK resource, attributes searched)
_SEARCH: dict[str, tuple[str, tuple[str, ...]]] = {
    "customer": ("customers", ("slug", "name", "domain", "external_customer_id")),
    "instance": ("instances", ("slug", "name", "description", "customer_slug")),
    "plan": ("license_families", ("slug",)),
    "license": ("licenses", ("slug", "name", "version_name", "description")),
    "entitlement": ("entitlements", ("slug", "name", "description")),
    "entitlement_group": ("entitlement_groups", ("slug", "name", "description")),
    "feature_flag": ("feature_flags", ("slug", "name", "description")),
    "deployment_zone": ("deployment_zones", ("slug", "name", "description", "type")),
    "release": ("releases", ("slug", "version", "description")),
    "component": ("components", ("slug", "name", "version")),
    "service_account": ("service_accounts", ("slug", "name")),
}


def _label(kind: str, item: Any) -> str | None:
    if kind == "plan":
        return item.current_version.name if item.current_version else None
    if kind == "release":
        return item.version
    if kind == "component":
        return f"{item.name}@{item.version}"
    return getattr(item, "name", None)


def register(mcp: FastMCP, *, writes: bool, ai_enabled: bool = False) -> None:
    @mcp.tool(annotations=READ)
    async def kaiten_overview(
        attention_within_days: Annotated[
            int, Field(ge=0, le=365, description="Flag licenses ending within this many days")
        ] = 30,
    ) -> dict:
        """Start here. A snapshot of the organization: how many customers, instances, plans, entitlements, flags,
        zones and releases it has; each plan's current version; what each zone runs; and the instances that need
        attention (unhealthy, license ending or ended, archived license, a limit in warning or overage)."""
        k = sdk.kaiten()
        customers, flags, accounts, families, zones, releases, snap = await asyncio.gather(
            k.customers.list(),
            k.feature_flags.list(),
            k.service_accounts.list(),
            k.license_families.list(),
            k.deployment_zones.list(),
            k.releases.list(),
            views.snapshot(ai_enabled=ai_enabled),
        )
        release_version = {r.id: r.version for r in releases}
        per_zone: dict[str, int] = {}
        for i in snap.instances:
            if i.deployment_zone_slug:
                per_zone[i.deployment_zone_slug] = per_zone.get(i.deployment_zone_slug, 0) + 1
        return {
            "counts": {
                "customers": len(customers),
                "instances": len(snap.instances),
                "plans": len(families),
                "entitlements": sum(
                    ai_policy.visible(e, enabled=ai_enabled) for e in snap.entitlements.values()
                ),
                "feature_flags": len(flags),
                "deployment_zones": len(zones),
                "releases": len(releases),
                "service_accounts": len(accounts),
            },
            "plans": [
                shapes.drop_none(
                    {
                        "plan": f.slug,
                        "current_version": f.current_version.slug if f.current_version else None,
                        "name": f.current_version.name if f.current_version else None,
                        "versions": f.version_count,
                    }
                )
                for f in families
            ],
            "deployment_zones": [
                shapes.drop_none(
                    {
                        "zone": z.slug,
                        "type": z.type,
                        "release": release_version.get(z.release_id),
                        "instances": per_zone.get(z.slug, 0),
                    }
                )
                for z in zones
            ],
            "attention": [
                {"instance": i.slug, "customer": i.customer_slug, "reasons": r}
                for i in snap.instances
                if (r := views.attention(snap, i, expiring_within_days=attention_within_days))
            ],
        }

    @mcp.tool(annotations=READ)
    async def kaiten_search(
        query: Annotated[
            str,
            Field(
                min_length=1,
                description="Text to find in slugs, names, descriptions, domains and external ids (case-insensitive)",
            ),
        ],
        kinds: Annotated[
            list[Kind] | None, Field(description="Restrict to these kinds; all when omitted")
        ] = None,
        limit: Annotated[int, Field(ge=1, le=100)] = 25,
    ) -> dict:
        """Find anything in the organization by name or slug: customers, instances, plans, license versions,
        entitlements, groups, feature flags, zones, releases, components, service accounts. Returns the kind and
        slug of each match, which every other tool accepts."""
        k = sdk.kaiten()
        wanted = kinds or list(_SEARCH)
        needle = query.strip().lower()
        results = await asyncio.gather(*(getattr(k, _SEARCH[kind][0]).list() for kind in wanted))
        matches = []
        for kind, items in zip(wanted, results, strict=True):
            for item in items:
                if kind == "entitlement" and not ai_policy.visible(item, enabled=ai_enabled):
                    continue
                hay = [str(getattr(item, a, None) or "").lower() for a in _SEARCH[kind][1]]
                label = _label(kind, item)
                hay.append(str(label or "").lower())
                if any(needle in h for h in hay):
                    exact = needle in (str(item.slug).lower(), str(label or "").lower())
                    matches.append(
                        (
                            not exact,
                            kind,
                            shapes.drop_none({"kind": kind, "slug": item.slug, "name": label}),
                        )
                    )
        matches.sort(key=lambda m: (m[0], m[1]))
        return {"total": len(matches), "matches": [m[2] for m in matches[:limit]]}
