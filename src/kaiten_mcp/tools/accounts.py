# Copyright 2026 KAITEN INC
# SPDX-License-Identifier: Apache-2.0

"""Customers and their instances: who bought what, and where it runs."""

from __future__ import annotations

import asyncio
from typing import Annotated, Any

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from kaitencloud.types import InstanceStatus
from pydantic import Field

from .. import sdk, shapes, views
from . import READ, WRITE


def _comparable(i: Any) -> dict:
    """The instance fields an update can change, with timestamps as instants."""
    return {
        "name": i.name,
        "description": i.description or "",
        "license": i.license_slug,
        "deployment_zone": i.deployment_zone_slug,
        "metadata": i.metadata or {},
        "license_start": shapes.as_time(i.start_license_date),
        "license_end": shapes.as_time(i.end_license_date),
        "status": i.status,
        "lifecycle_stage": i.lifecycle_stage,
    }


async def _summary(inst: Any) -> dict:
    snap = await views.snapshot([inst])
    return views.instance_summary(snap, inst)


def register(mcp: FastMCP, *, writes: bool) -> None:
    @mcp.tool(annotations=READ)
    async def kaiten_get_customer(
        customer: Annotated[str, Field(description="Customer slug")],
    ) -> dict:
        """A customer's 360 view: who they are, every instance they run, the plan and license version each one is
        on, usage against every limit, the features they are entitled to, and what needs attention."""
        k = sdk.kaiten()
        cslug = shapes.slug(customer, "customer")
        c, instances = await asyncio.gather(
            sdk.require(k.customers.get(cslug), f"customer {cslug!r}"), k.instances.list()
        )
        snap = await views.snapshot(instances, keep=lambda i: i.customer_slug == cslug)
        return {
            "customer": shapes.customer(c),
            "instances": [views.instance_summary(snap, i) for i in snap.instances],
            "attention": {i.slug: r for i in snap.instances if (r := views.attention(snap, i))},
        }

    @mcp.tool(annotations=READ)
    async def kaiten_get_instance(
        instance: Annotated[str, Field(description="Instance slug")],
        recent_events: Annotated[
            int, Field(ge=0, le=50, description="How many recent audit events to include")
        ] = 5,
    ) -> dict:
        """Everything about one instance: its customer, license version and plan, deployment zone and the release
        running there, usage against each limit (with reset dates), entitled features and config, what needs
        attention, and its most recent audit events."""
        k = sdk.kaiten()
        inst = await views.instance(instance)
        snap = await views.snapshot([inst])
        out: dict[str, Any] = views.instance_summary(snap, inst)
        if inst.deployment_zone_slug:
            z = await k.deployment_zones.get(inst.deployment_zone_slug)
            out["deployment_zone"] = {"slug": z.slug, "name": z.name, "type": z.type}
            if z.release_id:
                rel = next((r for r in await k.releases.list() if r.id == z.release_id), None)
                if rel:
                    out["deployment_zone"]["release"] = rel.version
        out["attention"] = views.attention(snap, inst)
        if recent_events:
            events = await k.instances.list_audit_trails(inst.slug, limit=recent_events)
            out["recent_events"] = [
                {"event": e.event_name, "at": shapes.iso(e.timestamp)}
                for e in events[:recent_events]
            ]
        return out

    @mcp.tool(annotations=READ)
    async def kaiten_list_instances(
        customer: Annotated[str | None, Field(description="Only this customer's instances")] = None,
        plan: Annotated[
            str | None,
            Field(description="Only instances on this plan (license family slug), any version"),
        ] = None,
        license: Annotated[
            str | None, Field(description="Only instances on this exact license version slug")
        ] = None,
        deployment_zone: Annotated[
            str | None, Field(description="Only instances in this deployment zone")
        ] = None,
        status: Annotated[
            InstanceStatus | None, Field(description="Only instances with this operational status")
        ] = None,
        lifecycle_stage: Annotated[
            str | None, Field(description="Only instances at this commercial lifecycle stage")
        ] = None,
        license_ends_within_days: Annotated[
            int | None,
            Field(
                description="Only instances whose license ends within this many days (ended ones included)"
            ),
        ] = None,
        needs_attention: Annotated[
            bool,
            Field(
                description="Only instances that need a look: unhealthy, license ending in 30 days, archived license, or a limit in warning/overage"
            ),
        ] = False,
        limit: Annotated[int, Field(ge=1, le=500)] = 50,
    ) -> dict:
        """Find instances by customer, plan, license version, zone, status, lifecycle stage, license end date or
        attention. Returns one compact row each; use kaiten_get_instance for the full picture."""
        k = sdk.kaiten()

        def keep(i: Any) -> bool:
            left = shapes.days_until(i.end_license_date)
            return not (
                (customer and i.customer_slug != customer)
                or (license and i.license_slug != license)
                or (deployment_zone and i.deployment_zone_slug != deployment_zone)
                or (status and i.status != status)
                or (lifecycle_stage and i.lifecycle_stage != lifecycle_stage)
                or (
                    license_ends_within_days is not None
                    and (left is None or left > license_ends_within_days)
                )
            )

        snap = await views.snapshot(await k.instances.list(), keep=keep, with_usage=needs_attention)
        rows = []
        for i in snap.instances:
            if plan and snap.plan_of(i) != plan:
                continue
            reasons = views.attention(snap, i)
            if needs_attention and not reasons:
                continue
            row = views.instance_summary(snap, i, with_entitlements=False)
            row.pop("metadata", None)
            row.pop("description", None)
            if reasons:
                row["attention"] = reasons
            rows.append(row)
        return shapes.drop_none(
            {"total": len(rows), "instances": rows[:limit], "truncated": len(rows) > limit or None}
        )

    if not writes:
        return

    @mcp.tool(annotations=WRITE)
    async def kaiten_save_customer(
        customer: Annotated[
            str, Field(description="Customer slug. Created when it does not exist yet")
        ],
        name: Annotated[str | None, Field(description="Display name (required to create)")] = None,
        domain: Annotated[
            str | None, Field(description="The customer's domain, e.g. acme.com")
        ] = None,
        external_customer_id: Annotated[
            str | None,
            Field(description="The id your CRM or billing system uses for this customer"),
        ] = None,
    ) -> dict:
        """Create a customer, or update the fields you pass on an existing one (others are kept).
        Reports created / updated / unchanged, and exactly which fields changed."""
        k = sdk.kaiten()
        cslug = shapes.slug(customer, "customer")
        cur = await sdk.get_or_none(k.customers.get(cslug))
        if cur is None:
            if not name:
                raise ToolError(f"Customer {cslug!r} does not exist yet: pass a name to create it.")
            created = await k.customers.create(
                name=name, slug=cslug, domain=domain, external_customer_id=external_customer_id
            )
            return shapes.report("created", "customer", shapes.customer(created))
        wanted = {
            "name": cur.name if name is None else name,
            "domain": cur.domain if domain is None else domain,
            "external_customer_id": cur.external_customer_id
            if external_customer_id is None
            else external_customer_id,
        }
        if wanted == {
            "name": cur.name,
            "domain": cur.domain,
            "external_customer_id": cur.external_customer_id,
        }:
            return shapes.report("unchanged", "customer", shapes.customer(cur))
        await k.customers.update(cslug, **wanted)
        after = await k.customers.get(cslug)
        diff = shapes.changes(shapes.customer(cur), shapes.customer(after))
        return shapes.report(
            "updated" if diff else "unchanged", "customer", shapes.customer(after), changes=diff
        )

    @mcp.tool(annotations=WRITE)
    async def kaiten_provision_instance(
        customer: Annotated[str, Field(description="Slug of the customer the instance belongs to")],
        name: Annotated[str, Field(description="Instance display name, e.g. 'Acme Production'")],
        plan: Annotated[
            str | None,
            Field(
                description="Plan (license family slug); the instance gets the plan's current version"
            ),
        ] = None,
        license: Annotated[
            str | None, Field(description="Exact license version slug, instead of plan")
        ] = None,
        slug: Annotated[
            str | None, Field(description="Instance slug; generated from the name when omitted")
        ] = None,
        deployment_zone: Annotated[
            str | None, Field(description="Deployment zone slug the instance runs in")
        ] = None,
        license_start: Annotated[
            str | None, Field(description="License start, ISO date; defaults to now")
        ] = None,
        license_end: Annotated[
            str | None, Field(description="License end, ISO date; defaults to start + term_days")
        ] = None,
        term_days: Annotated[
            int, Field(ge=1, description="License term when license_end is omitted")
        ] = 365,
        description: Annotated[str, Field(description="Free-text description")] = "",
        metadata: Annotated[
            dict[str, Any] | None,
            Field(
                description="Instance metadata, validated against the organization's INSTANCE metadata fields"
            ),
        ] = None,
        customer_name: Annotated[
            str | None,
            Field(description="Creates the customer with this name if it does not exist yet"),
        ] = None,
    ) -> dict:
        """Give a customer a new instance on a plan: resolves the plan to its current license version, optionally
        places it in a deployment zone, and sets its license term. Creates the customer too when customer_name is
        given and the customer is new. Subject to the organization's own limits."""
        k = sdk.kaiten()
        cslug = shapes.slug(customer, "customer")
        notes = []
        cust = await sdk.get_or_none(k.customers.get(cslug))
        if cust is None:
            if not customer_name:
                raise ToolError(
                    f"Customer {cslug!r} does not exist. Pass customer_name to create it, or use kaiten_save_customer."
                )
            cust = await k.customers.create(name=customer_name, slug=cslug)
            notes.append(f"created customer {cslug}")
        lic = await views.resolve_license(plan=plan, license=license)
        zone_id = None
        if deployment_zone:
            zslug = shapes.slug(deployment_zone, "deployment_zone")
            zone_id = (
                await sdk.require(k.deployment_zones.get(zslug), f"deployment zone {zslug!r}")
            ).id
        start = shapes.parse_time(license_start, "license_start") if license_start else shapes.now()
        end = (
            shapes.parse_time(license_end, "license_end")
            if license_end
            else shapes.plus_days(start, term_days)
        )
        if end <= start:
            raise ToolError("license_end must be after license_start.")
        created = await k.instances.create(
            name=name,
            slug=shapes.slug(slug, "instance") if slug else None,
            description=description,
            customer_id=cust.id,
            license_id=lic.id,
            deployment_zone_id=zone_id,
            start_license_date=start,
            end_license_date=end,
            metadata=metadata,
        )
        return shapes.report("created", "instance", await _summary(created), notes=notes)

    @mcp.tool(annotations=WRITE)
    async def kaiten_update_instance(
        instance: Annotated[str, Field(description="Instance slug")],
        name: str | None = None,
        description: str | None = None,
        plan: Annotated[
            str | None,
            Field(
                description="Move to this plan's current version (license family slug): an upgrade, downgrade or catch-up to the latest version"
            ),
        ] = None,
        license: Annotated[
            str | None,
            Field(description="Move to this exact license version slug, instead of plan"),
        ] = None,
        deployment_zone: Annotated[
            str | None,
            Field(
                description="Deploy to (or migrate to) this zone slug. Kaiten has no way to take an instance out of its zone"
            ),
        ] = None,
        license_start: Annotated[
            str | None, Field(description="New license start, ISO date")
        ] = None,
        license_end: Annotated[str | None, Field(description="New license end, ISO date")] = None,
        extend_license_days: Annotated[
            int | None, Field(description="Push the license end out by this many days (a renewal)")
        ] = None,
        metadata: Annotated[
            dict[str, Any] | None,
            Field(description="Keys to set; a key set to null is removed. Other keys are kept"),
        ] = None,
        status: Annotated[InstanceStatus | None, Field(description="Operational status")] = None,
        lifecycle_stage: Annotated[
            str | None,
            Field(description="Commercial lifecycle stage, e.g. onboarding, active, churning"),
        ] = None,
    ) -> dict:
        """Change an instance: rename it, change its plan or license version, deploy or migrate it to a zone, renew or
        move its license term, edit metadata keys, or set its status and lifecycle stage. Only what you pass changes.
        The answer reports what Kaiten now holds: a call that changes nothing says 'unchanged'."""
        if license_end and extend_license_days:
            raise ToolError("Pass license_end or extend_license_days, not both.")
        k = sdk.kaiten()
        cur = await views.instance(instance)

        license_id = cur.license_id
        if plan or license:
            license_id = (await views.resolve_license(plan=plan, license=license)).id
        zone_id = cur.deployment_zone_id
        if deployment_zone is not None:
            zslug = shapes.slug(deployment_zone, "deployment_zone")
            zone_id = (
                await sdk.require(k.deployment_zones.get(zslug), f"deployment zone {zslug!r}")
            ).id
        start = (
            shapes.parse_time(license_start, "license_start")
            if license_start
            else shapes.as_time(cur.start_license_date)
        )
        end = shapes.as_time(cur.end_license_date)
        if license_end:
            end = shapes.parse_time(license_end, "license_end")
        if extend_license_days:
            end = shapes.plus_days(end, extend_license_days)
        new = {
            "name": cur.name if name is None else name,
            "description": (cur.description or "") if description is None else description,
            "license_id": license_id,
            "deployment_zone_id": zone_id,
            "start": start,
            "end": end,
            "metadata": shapes.merge_map(cur.metadata, metadata),
        }
        old = {
            "name": cur.name,
            "description": cur.description or "",
            "license_id": cur.license_id,
            "deployment_zone_id": cur.deployment_zone_id,
            "start": shapes.as_time(cur.start_license_date),
            "end": shapes.as_time(cur.end_license_date),
            "metadata": cur.metadata or {},
        }
        if new != old:
            await k.instances.update(
                cur.slug,
                name=new["name"],
                description=new["description"],
                customer_id=cur.customer_id,
                license_id=new["license_id"],
                deployment_zone_id=new["deployment_zone_id"],
                start_license_date=new["start"],
                end_license_date=new["end"],
                metadata=new["metadata"],
            )
        if status and status != cur.status:
            await k.instances.update_status(cur.slug, status)
        if lifecycle_stage is not None and lifecycle_stage != cur.lifecycle_stage:
            await k.instances.update_lifecycle_stage(cur.slug, lifecycle_stage)

        # Report what Kaiten now holds, not what was asked: a field the API
        # kept as it was must never be announced as changed.
        after = await k.instances.get(cur.slug)
        actual = shapes.changes(_comparable(cur), _comparable(after))
        for _key, change in actual.items():
            for end_ in ("from", "to"):
                if hasattr(change[end_], "isoformat"):
                    change[end_] = shapes.rfc3339(change[end_])
        notes = []
        if "deployment_zone" in actual:
            notes.append(
                "deployed to a zone (INSTANCE_DEPLOYED)"
                if actual["deployment_zone"]["from"] is None
                else "migrated between zones (INSTANCE_MIGRATED)"
            )
        if "license" in actual:
            notes.append("usage is now measured against the new license version's grants")
        return shapes.report(
            "updated" if actual else "unchanged",
            "instance",
            await _summary(after),
            changes=actual,
            notes=notes,
        )
