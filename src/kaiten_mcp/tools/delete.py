# Copyright 2026 KAITEN INC
# SPDX-License-Identifier: Apache-2.0

"""Deletion, traced first.

An agent traces before it changes: without confirm, this tool only reports
what a deletion would touch. With confirm, it deletes, and a deletion Kaiten
refuses (something still depends on it) comes back as 'rejected' with that
same impact, rather than as an opaque error.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from fastmcp import FastMCP
from kaitencloud import ConflictError
from pydantic import Field

from .. import ai_policy, sdk, shapes, views
from . import DESTRUCTIVE

Kind = Literal[
    "customer",
    "instance",
    "license",
    "entitlement",
    "entitlement_group",
    "feature_flag",
    "deployment_zone",
    "release",
    "component",
]

# kind -> SDK resource holding get() and delete()
_RESOURCE = {
    "customer": "customers",
    "instance": "instances",
    "license": "licenses",
    "entitlement": "entitlements",
    "entitlement_group": "entitlement_groups",
    "feature_flag": "feature_flags",
    "deployment_zone": "deployment_zones",
    "release": "releases",
    "component": "components",
}


async def impact(kind: str, slug: str, resource: Any, *, ai_enabled: bool = False) -> dict:
    """What depends on the resource, as far as the API lets us see."""
    k = sdk.kaiten()
    if kind in ("customer", "license", "deployment_zone"):
        attr = {
            "customer": "customer_slug",
            "license": "license_slug",
            "deployment_zone": "deployment_zone_slug",
        }[kind]
        out: dict[str, Any] = {
            "instances": [i.slug for i in await k.instances.list() if getattr(i, attr) == slug]
        }
        if kind == "license":
            definitions = {e.slug: e for e in await k.entitlements.list()}
            out["grants"] = [
                g.entitlement_slug
                for g in await k.licenses.list_entitlements(slug)
                if ai_policy.visible(
                    g, enabled=ai_enabled, definition=definitions.get(g.entitlement_slug)
                )
            ]
        return out
    if kind == "instance":
        snap = await views.snapshot([resource], ai_enabled=ai_enabled)
        used = [
            r["entitlement"]
            for r in views.entitlements_view(snap, resource)["limits"]
            if r.get("used")
        ]
        return {"customer": resource.customer_slug, "usage_history_lost_for": used}
    if kind == "entitlement":
        licenses = await k.licenses.list()
        grants = await views.gather_bounded(
            k.licenses.list_entitlements(lic.slug) for lic in licenses
        )
        flags = await k.feature_flags.list()
        return {
            "granted_by_licenses": [
                lic.slug
                for lic, gs in zip(licenses, grants, strict=True)
                if any(g.entitlement_slug == slug for g in gs)
            ],
            "flags_reading_it": [
                f.slug
                for f in flags
                if any(slug in (t.get("rule") or "") for t in f.to_dict().get("targetings") or [])
            ],
        }
    if kind == "release":
        return {
            "running_in_zones": [
                z.slug for z in await k.deployment_zones.list() if z.release_id == resource.id
            ]
        }
    if kind == "component":
        return {
            "in_releases": [
                r.version
                for r in await views.releases()
                if any(c.slug == slug for c in r.components or [])
            ]
        }
    if kind == "feature_flag":
        return {
            "note": "applications evaluating this flag will get FLAG_NOT_FOUND and fall back to their code default"
        }
    return {}


def register(mcp: FastMCP, *, writes: bool, ai_enabled: bool = False) -> None:
    if not writes:
        return

    @mcp.tool(annotations=DESTRUCTIVE)
    async def kaiten_delete(
        kind: Annotated[Kind, Field(description="What to delete")],
        slug: Annotated[str, Field(description="Its slug")],
        confirm: Annotated[
            bool,
            Field(
                description="false (default): only report what the deletion would affect. true: delete"
            ),
        ] = False,
    ) -> dict:
        """Delete a customer, instance, license version, entitlement, group, flag, zone, release or component. Call it
        first without confirm to see what depends on it, then again with confirm=true. Kaiten refuses to delete what
        is still in use (a license an instance is on, a release a zone runs, an entitlement a license grants); that
        comes back as 'rejected' with the dependents to deal with first."""
        resource_api = getattr(sdk.kaiten(), _RESOURCE[kind])
        s = shapes.slug(slug, kind)
        resource = await sdk.require(resource_api.get(s), f"{kind} {s!r}")
        if kind == "entitlement":
            ai_policy.require_available(resource, enabled=ai_enabled)
        if confirm and not ai_enabled:
            if kind == "license":
                await ai_policy.require_license_available(s, enabled=ai_enabled)
            elif kind == "instance":
                await ai_policy.require_license_available(resource.license_slug, enabled=ai_enabled)
            elif kind == "customer":
                for inst in await sdk.kaiten().instances.list():
                    if inst.customer_slug == s:
                        await ai_policy.require_license_available(
                            inst.license_slug, enabled=ai_enabled
                        )
            elif kind == "entitlement_group":
                for e in await sdk.kaiten().entitlements.list():
                    if any(g.slug == s for g in e.entitlement_groups or []):
                        ai_policy.require_available(e, enabled=ai_enabled)
        label = getattr(resource, "name", None) or getattr(resource, "version", None) or s
        blast = {
            key: v
            for key, v in (await impact(kind, s, resource, ai_enabled=ai_enabled)).items()
            if v
        }
        if not confirm:
            return shapes.report(
                "preview",
                kind,
                {"slug": s, "name": label},
                impact=blast,
                notes=["Nothing was deleted. Call again with confirm=true to delete."],
            )
        try:
            await resource_api.delete(s)
        except ConflictError as e:
            return shapes.report(
                "rejected",
                kind,
                {"slug": s, "name": label},
                reason=f"{e.code}: {e.detail}",
                impact=blast,
            )
        return shapes.report("deleted", kind, {"slug": s, "name": label}, impact=blast)
