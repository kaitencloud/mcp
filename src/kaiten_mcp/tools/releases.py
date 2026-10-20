# Copyright 2026 KAITEN INC
# SPDX-License-Identifier: Apache-2.0

"""Release orchestration: components, immutable releases, and the zones running them."""

from __future__ import annotations

import asyncio
from typing import Annotated, Any

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from pydantic import Field

from .. import sdk, shapes, views
from . import READ, WRITE


def _component_spec(c: Any) -> tuple[str, str]:
    if isinstance(c, str) and "@" in c:
        name, version = c.rsplit("@", 1)
        return name.strip(), version.strip()
    if isinstance(c, dict) and c.get("name") and c.get("version"):
        return str(c["name"]), str(c["version"])
    raise ToolError(f"A component is 'name@version' or {{name, version}}, got {c!r}")


def register(mcp: FastMCP, *, writes: bool) -> None:
    @mcp.tool(annotations=READ)
    async def kaiten_get_deployments(
        deployment_zone: Annotated[
            str | None, Field(description="A zone slug, for its release and instances")
        ] = None,
    ) -> dict:
        """What runs where. Without a zone: every deployment zone with the release it runs and how many instances it
        holds, and every release (newest first) with its components and the zones running it. With a zone: its
        release and components, and its instances."""
        k = sdk.kaiten()
        zones, releases, instances = await asyncio.gather(
            k.deployment_zones.list(), views.releases(), k.instances.list()
        )
        by_id = {r.id: r for r in releases}
        in_zone: dict[str, list[str]] = {}
        for i in instances:
            if i.deployment_zone_slug:
                in_zone.setdefault(i.deployment_zone_slug, []).append(i.slug)

        if deployment_zone:
            z = next((z for z in zones if z.slug == deployment_zone), None)
            if z is None:
                raise ToolError(
                    f"No deployment zone {deployment_zone!r}. Zones: {', '.join(z.slug for z in zones) or 'none'}."
                )
            rel = by_id.get(z.release_id)
            return {
                **shapes.zone(z),
                "release": shapes.release(rel) if rel else None,
                "instances": in_zone.get(z.slug, []),
            }

        return {
            "deployment_zones": [
                {
                    **shapes.zone(z),
                    "release": by_id[z.release_id].version if z.release_id in by_id else None,
                    "instances": len(in_zone.get(z.slug, [])),
                }
                for z in zones
            ],
            "releases": [
                {
                    **shapes.release(r),
                    "running_in": sorted(z.slug for z in zones if z.release_id == r.id),
                }
                for r in sorted(releases, key=lambda r: r.created_at, reverse=True)
            ],
        }

    if not writes:
        return

    @mcp.tool(annotations=WRITE)
    async def kaiten_create_release(
        version: Annotated[str, Field(description="Release version, e.g. '2026.9.0'")],
        components: Annotated[
            list[Any],
            Field(
                description="Components it ships, each 'name@version' or {name, version}. Missing ones are created, chained to the previous version of the same name"
            ),
        ],
        description: str = "",
        slug: Annotated[
            str | None, Field(description="Release slug; generated from the version when omitted")
        ] = None,
    ) -> dict:
        """Cut a release: register any component version that does not exist yet and create the release that ships
        them. A release is immutable; asking for one that already exists with the same components reports
        'unchanged', and with different components is refused. Deploy it with kaiten_deploy_release."""
        k = sdk.kaiten()
        wanted = [_component_spec(c) for c in components]
        if not wanted:
            raise ToolError("A release ships at least one component.")
        existing, releases = await asyncio.gather(k.components.list(), views.releases())
        same = next((r for r in releases if r.version == version), None)
        if same:
            have = sorted((c.name, c.version) for c in same.components or [])
            if have == sorted(wanted):
                return shapes.report("unchanged", "release", shapes.release(same))
            raise ToolError(
                f"Release {version} already exists with components {[f'{n}@{v}' for n, v in have]}. Releases are immutable: use a new version."
            )
        notes, ids = [], []
        for name, ver in wanted:
            match = next((c for c in existing if c.name == name and c.version == ver), None)
            if match is None:
                previous = max(
                    (c for c in existing if c.name == name),
                    key=lambda c: c.created_at,
                    default=None,
                )
                match = await k.components.create(
                    name=name, version=ver, previous_component_id=previous.id if previous else None
                )
                existing.append(match)
                notes.append(f"registered component {name}@{ver}")
            ids.append(match.id)
        created = await k.releases.create(
            version=version,
            description=description,
            component_ids=ids,
            slug=shapes.slug(slug, "release") if slug else None,
        )
        return shapes.report(
            "created", "release", shapes.release(await k.releases.get(created.slug)), notes=notes
        )

    @mcp.tool(annotations=WRITE)
    async def kaiten_deploy_release(
        deployment_zone: Annotated[str, Field(description="Deployment zone slug")],
        release: Annotated[
            str,
            Field(
                description="Release slug or version to put on the zone; a previous one is a rollback"
            ),
        ],
    ) -> dict:
        """Record that a zone now runs a release; Kaiten appends it to the zone's deployment history. Reports the release
        it replaced and the instances in the zone; a zone already on that release is 'unchanged'."""
        k = sdk.kaiten()
        zslug = shapes.slug(deployment_zone, "deployment_zone")
        z, releases = await asyncio.gather(
            sdk.require(k.deployment_zones.get(zslug), f"deployment zone {zslug!r}"),
            k.releases.list(),
        )
        target = next((r for r in releases if release in (r.slug, r.version)), None)
        if target is None:
            raise ToolError(
                f"No release {release!r}. Releases: {', '.join(r.version for r in releases) or 'none'}."
            )
        previous = next((r for r in releases if r.id == z.release_id), None)
        if previous and previous.id == target.id:
            return shapes.report(
                "unchanged", "deployment_zone", {**shapes.zone(z), "release": target.version}
            )
        await k.deployment_zones.update(
            z.slug,
            name=z.name,
            type=z.type,
            description=z.description or "",
            metadata=z.metadata,
            release_id=target.id,
        )
        affected = [i.slug for i in await k.instances.list() if i.deployment_zone_slug == z.slug]
        return shapes.report(
            "updated",
            "deployment_zone",
            {**shapes.zone(z), "release": target.version},
            changes={
                "release": {"from": previous.version if previous else None, "to": target.version}
            },
            affected_instances=affected or None,
        )

    @mcp.tool(annotations=WRITE)
    async def kaiten_save_deployment_zone(
        deployment_zone: Annotated[
            str, Field(description="Zone slug. Created when it does not exist yet")
        ],
        name: Annotated[str | None, Field(description="Display name (required to create)")] = None,
        type: Annotated[
            str | None,
            Field(
                description="How you classify it, e.g. production, staging, dedicated (defaults to production)"
            ),
        ] = None,
        description: str | None = None,
        metadata: Annotated[
            dict[str, Any] | None,
            Field(
                description="Keys to set; null removes a key. Validated against DEPLOYMENT_ZONE metadata fields"
            ),
        ] = None,
    ) -> dict:
        """Create a deployment zone (a place customers' instances run), or change the fields you pass on one. Its
        release is changed with kaiten_deploy_release, which keeps the history."""
        k = sdk.kaiten()
        zslug = shapes.slug(deployment_zone, "deployment_zone")
        cur = await sdk.get_or_none(k.deployment_zones.get(zslug))
        if cur is None:
            if not name:
                raise ToolError(f"Zone {zslug!r} does not exist yet: pass a name to create it.")
            created = await k.deployment_zones.create(
                slug=zslug,
                name=name,
                type=type or "production",
                description=description or "",
                metadata=shapes.merge_map({}, metadata) or None,
            )
            return shapes.report("created", "deployment_zone", shapes.zone(created))
        wanted = {
            "name": cur.name if name is None else name,
            "type": cur.type if type is None else type,
            "description": (cur.description or "") if description is None else description,
            "metadata": shapes.merge_map(cur.metadata, metadata),
        }
        if wanted == {
            "name": cur.name,
            "type": cur.type,
            "description": cur.description or "",
            "metadata": cur.metadata or {},
        }:
            return shapes.report("unchanged", "deployment_zone", shapes.zone(cur))
        await k.deployment_zones.update(zslug, **wanted)
        after = await k.deployment_zones.get(zslug)
        return shapes.report(
            "updated",
            "deployment_zone",
            shapes.zone(after),
            changes=shapes.changes(shapes.zone(cur), shapes.zone(after)),
        )
