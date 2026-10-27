# Copyright 2026 KAITEN INC
# SPDX-License-Identifier: Apache-2.0

"""The catalog: entitlements (what can be sold) and plans (what is sold).

A plan is a license family; each of its versions is a license carrying grants,
one value per entitlement. Instances sit on one exact version, so editing a
version in place changes every instance on it at once, while publishing a new
version changes nobody until an instance is moved to it.
"""

from __future__ import annotations

import asyncio
from typing import Annotated, Any, Literal

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from kaitencloud.types import (
    AggregationMethod,
    EntitlementType,
    LicenseType,
    ResetAnchor,
    ResetPeriod,
)
from pydantic import Field

from .. import ai_policy, sdk, shapes, views
from . import READ, WRITE

GrantSpec = Annotated[
    dict[str, Any] | None,
    Field(
        description=(
            "Grants by entitlement slug. A value is a number (NUMBER: -1 or 'unlimited' for no cap), true/false "
            "(BOOLEAN), or an object (CONFIG); or {'value': v, 'overage_percent': p} where p=0 is a hard limit and "
            "p>0 lets usage run p% past the value. null removes the grant."
        )
    ),
]


def _parse_grant(spec: Any) -> tuple[Any, int | None]:
    if isinstance(spec, dict) and "value" in spec and set(spec) <= {"value", "overage_percent"}:
        value, overage = shapes.grant_value(spec["value"]), spec.get("overage_percent")
    else:
        value, overage = shapes.grant_value(spec), None
    if isinstance(value, int | float) and not isinstance(value, bool) and value == shapes.UNLIMITED:
        overage = shapes.UNLIMITED
    elif overage is not None and (type(overage) is not int or overage < 0):
        raise ToolError("overage_percent must be a non-negative integer.")
    return value, overage


def _check_type(ent: Any, value: Any) -> None:
    kind = ent.type or "BOOLEAN"
    ok = (
        isinstance(value, bool)
        if kind == "BOOLEAN"
        else isinstance(value, dict)
        if kind == "CONFIG"
        else isinstance(value, int | float) and not isinstance(value, bool)
    )
    if not ok:
        expected = {"BOOLEAN": "true or false", "CONFIG": "a JSON object"}.get(
            kind, "a number (-1 or 'unlimited' for no cap)"
        )
        raise ToolError(f"{ent.slug} is a {kind} entitlement: its value must be {expected}.")


async def _prepare_grants(
    grants: dict[str, Any], existing: dict[str, Any], *, ai_enabled: bool, creating: bool = False
) -> dict[str, tuple[Any, int | None] | None]:
    """Validate the entire patch before a license or any grant is written.

    Keep canonical grants separate from visible projections: omitted grants are
    unchanged, including relations hidden from operational discovery.
    """
    if not grants:
        return {}
    catalog = {e.slug: e for e in await sdk.kaiten().entitlements.list()}
    prepared = {}
    for raw_slug, spec in grants.items():
        ent_slug = shapes.slug(raw_slug, "entitlement")
        definition = catalog.get(ent_slug)
        cur = existing.get(ent_slug)
        ai_policy.require_available(cur, enabled=ai_enabled, definition=definition)
        if definition is None:
            if creating and spec is None and cur is None:
                continue  # Creation has nothing to remove; preserve the ordinary null no-op.
            raise ToolError(
                f"No entitlement {ent_slug!r}. Create it with kaiten_save_entitlement first."
            )
        if spec is None:
            prepared[ent_slug] = None
        else:
            value, overage = _parse_grant(spec)
            _check_type(definition, value)
            prepared[ent_slug] = (value, overage)
    return prepared


async def _apply_grants(
    license_slug: str, prepared: dict[str, tuple[Any, int | None] | None], existing: dict[str, Any]
) -> dict[str, dict]:
    """Apply an already validated patch without deriving writes from a filtered view."""
    k = sdk.kaiten()
    diff: dict[str, dict] = {}
    for ent_slug, spec in prepared.items():
        cur = existing.get(ent_slug)
        if spec is None:
            if cur is not None:
                await k.licenses.delete_entitlement(license_slug, ent_slug)
                diff[ent_slug] = {"from": shapes.grant(cur), "to": None}
            continue
        value, overage = spec
        if cur is None:
            await k.licenses.associate_entitlement(
                license_slug, ent_slug, value, overage_percent=overage
            )
        else:
            if overage is None:
                overage = cur.limit_cap_exceeded_overage_percent
            if (
                shapes.plain(cur.value) == value
                and cur.limit_cap_exceeded_overage_percent == overage
            ):
                continue
            await k.licenses.update_entitlement(
                license_slug, ent_slug, value, overage_percent=overage
            )
        after = await k.licenses.get_entitlement(license_slug, ent_slug)
        diff[ent_slug] = {"from": shapes.grant(cur) if cur else None, "to": shapes.grant(after)}
    return diff


async def _grants_of(license_slug: str) -> dict[str, Any]:
    return {
        g.entitlement_slug: g for g in await sdk.kaiten().licenses.list_entitlements(license_slug)
    }


async def _version_view(
    lic: Any,
    instances: list[Any],
    *,
    list_instances: bool,
    plan: str | None,
    ai_enabled: bool = False,
    definitions: dict[str, Any] | None = None,
) -> dict:
    out = shapes.license_(lic, plan)
    # LicenseEntitlement.entitlementType is the original catalogue discriminator;
    # even older SDKs retain unknown string values. Do not require a new catalogue
    # read permission merely to render a license view (including after a write).
    out["grants"] = [
        shapes.grant(g)
        for g in (await _grants_of(lic.slug)).values()
        if ai_policy.visible(
            g, enabled=ai_enabled, definition=(definitions or {}).get(g.entitlement_slug)
        )
    ]
    on_it = [i.slug for i in instances if i.license_slug == lic.slug]
    if list_instances:
        out["instances"] = on_it
    else:
        out["instance_count"] = len(on_it)
    return out


def register(mcp: FastMCP, *, writes: bool, ai_enabled: bool = False) -> None:
    @mcp.tool(annotations=READ)
    async def kaiten_get_catalog(
        plan: Annotated[
            str | None,
            Field(
                description="A plan (license family slug): show every version with its state, instances and grants"
            ),
        ] = None,
    ) -> dict:
        """What the organization sells. Without a plan: the entitlement catalog (with groups) and every plan's
        current version with its grants, as a plan comparison. With a plan: that plan's versions, oldest first,
        each with its lifecycle state, the instances on it, and its grants."""
        k = sdk.kaiten()
        instances = await k.instances.list()
        if plan:
            fam = await sdk.require(
                k.license_families.get(shapes.slug(plan, "plan"), include_versions=True),
                f"plan {plan!r}",
            )
            versions = sorted(fam.versions or [], key=lambda v: int(v.version))
            return {
                "plan": fam.slug,
                "current_version": fam.current_version.slug if fam.current_version else None,
                "versions": await views.gather_bounded(
                    _version_view(
                        v, instances, list_instances=True, plan=fam.slug, ai_enabled=ai_enabled
                    )
                    for v in versions
                ),
            }
        ents, groups, families = await asyncio.gather(
            k.entitlements.list(), k.entitlement_groups.list(), k.license_families.list()
        )
        definitions = {e.slug: e for e in ents}
        served = [f for f in families if f.current_version]
        current = await views.gather_bounded(
            _version_view(
                f.current_version,
                instances,
                list_instances=False,
                plan=f.slug,
                ai_enabled=ai_enabled,
                definitions=definitions,
            )
            for f in served
        )
        by_family = {f.slug: {**v, "plan": f.slug} for f, v in zip(served, current, strict=True)}
        return {
            "entitlements": [
                shapes.entitlement(e) for e in ents if ai_policy.visible(e, enabled=ai_enabled)
            ],
            "entitlement_groups": [{"slug": g.slug, "name": g.name} for g in groups],
            "plans": [
                shapes.drop_none(
                    {"plan": f.slug, "versions": f.version_count, "current": by_family.get(f.slug)}
                )
                for f in families
            ],
        }

    if not writes:
        return

    async def kaiten_save_entitlement(
        entitlement: Annotated[
            str, Field(description="Entitlement slug. Created when it does not exist yet")
        ],
        name: Annotated[str | None, Field(description="Display name (required to create)")] = None,
        type: Annotated[
            EntitlementType | None,
            Field(
                description="BOOLEAN feature, NUMBER quota, NUMBER_AI_CREDIT, or CONFIG value. Set at creation"
            ),
        ] = None,
        description: str | None = None,
        aggregation: Annotated[
            AggregationMethod | None,
            Field(description="How reports combine, for metered types (defaults to SUM)"),
        ] = None,
        reset_period: Annotated[
            ResetPeriod | None,
            Field(description="Usage resets every period; omit for a lifetime quota"),
        ] = None,
        reset_anchor: Annotated[
            ResetAnchor | None,
            Field(description="Periods align on the calendar or on each license's start"),
        ] = None,
        unit_singular: str | None = None,
        unit_plural: str | None = None,
        warning_threshold_percent: Annotated[
            int | None,
            Field(
                ge=0, le=100, description="Usage at this percentage of the limit raises a warning"
            ),
        ] = None,
        user_facing: Annotated[bool | None, Field(description="Shown to end customers")] = None,
        display_order: int | None = None,
        icon: str | None = None,
        groups: Annotated[
            list[str] | None,
            Field(
                description="The complete list of group slugs it belongs to; missing groups are created"
            ),
        ] = None,
    ) -> dict:
        """Define an entitlement in the catalog, or update the fields you pass on an existing one. groups sets its
        group membership exactly. Reports created / updated / unchanged with the changes."""
        k = sdk.kaiten()
        eslug = shapes.slug(entitlement, "entitlement")
        given = shapes.drop_none(
            {
                "name": name,
                "description": description,
                "type": type,
                "aggregation_method": aggregation,
                "reset_period": reset_period,
                "reset_anchor": reset_anchor,
                "unit_singular": unit_singular,
                "unit_plural": unit_plural,
                "warning_threshold_percent": warning_threshold_percent,
                "user_facing": user_facing,
                "display_order": display_order,
                "icon": icon,
            }
        )
        if type == "NUMBER_AI_CREDIT" and not ai_enabled:
            raise ToolError("This entitlement type is not available in this MCP deployment.")
        cur = await sdk.get_or_none(k.entitlements.get(eslug))
        ai_policy.require_available(cur, enabled=ai_enabled)
        effective_type = type or (cur.type if cur else None)
        effective_aggregation = aggregation or (cur.aggregation_method if cur else None) or "SUM"
        if effective_type == "NUMBER_AI_CREDIT" and effective_aggregation != "SUM":
            raise ToolError("NUMBER_AI_CREDIT entitlements require SUM aggregation.")
        status = "updated"
        if cur is None:
            if not name or not type:
                raise ToolError(
                    f"Entitlement {eslug!r} does not exist yet: pass name and type to create it."
                )
            if type in shapes.METERED:
                given.setdefault("aggregation_method", "SUM")
            cur = await k.entitlements.create(slug=eslug, **given)
            status = "created"
        elif any(getattr(cur, f) != v for f, v in given.items()):
            fields = (
                "name",
                "description",
                "type",
                "aggregation_method",
                "reset_period",
                "reset_anchor",
                "unit_singular",
                "unit_plural",
                "sale_unit_singular",
                "sale_unit_plural",
                "sale_unit_factor",
                "warning_threshold_percent",
                "user_facing",
                "display_order",
                "icon",
            )
            await k.entitlements.update(eslug, **{**{f: getattr(cur, f) for f in fields}, **given})

        if groups is not None:
            have = {g.slug for g in cur.entitlement_groups or []}
            want = {shapes.slug(g, "group") for g in groups}
            existing = {g.slug for g in await k.entitlement_groups.list()}
            for g in sorted(want - have):
                if g not in existing:
                    await k.entitlement_groups.create(slug=g, name=g.replace("-", " ").title())
                await k.entitlement_groups.add_entitlement(g, eslug)
            for g in sorted(have - want):
                await k.entitlement_groups.remove_entitlement(g, eslug)

        after = await k.entitlements.get(eslug)
        if status == "created":
            return shapes.report("created", "entitlement", shapes.entitlement(after))
        diff = shapes.changes(shapes.entitlement(cur), shapes.entitlement(after))
        return shapes.report(
            "updated" if diff else "unchanged",
            "entitlement",
            shapes.entitlement(after),
            changes=diff,
        )

    # Project only this tool's write discriminator; do not mutate the SDK's enum.
    if not ai_enabled:
        kaiten_save_entitlement.__annotations__["type"] = Annotated[
            Literal["BOOLEAN", "NUMBER", "CONFIG"] | None,
            Field(description="BOOLEAN feature, NUMBER quota, or CONFIG value. Set at creation"),
        ]
    mcp.tool(annotations=WRITE)(kaiten_save_entitlement)

    @mcp.tool(annotations=WRITE)
    async def kaiten_save_license(
        license: Annotated[
            str | None, Field(description="Edit this existing license version in place (its slug)")
        ] = None,
        new_version_of: Annotated[
            str | None, Field(description="Add the next version to this plan (license family slug)")
        ] = None,
        name: Annotated[
            str | None,
            Field(
                description="Display name. Required for a new plan; a new version keeps the plan's name"
            ),
        ] = None,
        description: str | None = None,
        type: Annotated[LicenseType | None, Field(description="Required for a new plan")] = None,
        version_name: Annotated[
            str | None, Field(description="Label for this version, e.g. '2026 pricing'")
        ] = None,
        slug: Annotated[
            str | None, Field(description="Slug of a new plan (it becomes the plan's slug too)")
        ] = None,
        draft: Annotated[
            bool, Field(description="Create the version as a DRAFT, not on sale until published")
        ] = False,
        grants: GrantSpec = None,
        copy_grants: Annotated[
            bool,
            Field(
                description="For new_version_of: start from the plan's current version's grants, then apply grants on top"
            ),
        ] = True,
    ) -> dict:
        """Shape what a plan includes. Three intents:
        - new plan: neither license nor new_version_of; creates a license family with version 1.
        - new version of a plan: new_version_of=<plan>; copies the current version's grants (unless copy_grants is
          false) and applies yours. Nobody moves to it until an instance is updated, so it is the safe way to change
          pricing.
        - edit a version in place: license=<version slug>; changes every instance on that version immediately, and
          the answer lists them.
        Grants not mentioned are kept; null removes one."""
        if license and new_version_of:
            raise ToolError(
                "Pass license (edit a version) or new_version_of (add a version), not both."
            )
        k = sdk.kaiten()
        notes: list[str] = []

        if license:
            lslug = shapes.slug(license, "license")
            cur = await sdk.require(k.licenses.get(lslug), f"license {lslug!r}")
            existing_grants = await _grants_of(lslug)
            prepared = await _prepare_grants(grants or {}, existing_grants, ai_enabled=ai_enabled)
            wanted = {
                "name": cur.name if name is None else name,
                "description": cur.description if description is None else description,
                "type": type or cur.type,
                "version_name": cur.version_name if version_name is None else version_name,
            }
            if wanted != {
                "name": cur.name,
                "description": cur.description,
                "type": cur.type,
                "version_name": cur.version_name,
            }:
                await k.licenses.update(lslug, is_default=cur.is_default, **wanted)
            grant_diff = await _apply_grants(lslug, prepared, existing_grants)
            after = await k.licenses.get(lslug)
            plan = (await views.plan_slugs()).get(after.family_id)
            diff: dict[str, Any] = shapes.changes(
                shapes.license_(cur, plan), shapes.license_(after, plan)
            )
            if grant_diff:
                diff["grants"] = grant_diff
            if not diff:
                return shapes.report("unchanged", "license", shapes.license_(after, plan))
            affected = [i.slug for i in await k.instances.list() if i.license_slug == lslug]
            if grant_diff and affected:
                notes.append(f"{len(affected)} instance(s) on this version see the new grants now")
            return shapes.report(
                "updated",
                "license",
                shapes.license_(after, plan),
                changes=diff,
                affected_instances=affected or None,
                notes=notes,
            )

        if new_version_of:
            fam = await sdk.require(
                k.license_families.get(shapes.slug(new_version_of, "plan"), include_versions=True),
                f"plan {new_version_of!r}",
            )
            base = fam.current_version or max(
                fam.versions or [], key=lambda v: int(v.version), default=None
            )
            start: dict[str, Any] = {}
            if copy_grants and base:
                source_grants = await _grants_of(base.slug)
                source_definitions = {e.slug: e for e in await k.entitlements.list()}
                for g in source_grants.values():
                    ai_policy.require_available(
                        g, enabled=ai_enabled, definition=source_definitions.get(g.entitlement_slug)
                    )
                    start[g.entitlement_slug] = (
                        {
                            "value": shapes.plain(g.value),
                            "overage_percent": g.limit_cap_exceeded_overage_percent,
                        }
                        if g.limit_cap_exceeded_overage_percent is not None
                        else shapes.plain(g.value)
                    )
                notes.append(f"grants copied from {base.slug}")
            prepared = await _prepare_grants(
                {**start, **(grants or {})}, {}, ai_enabled=ai_enabled, creating=True
            )
            created = await k.licenses.create(
                family_slug=fam.slug,
                name=name or (base.name if base else fam.slug),
                description=description
                if description is not None
                else (base.description if base else ""),
                type=type or (base.type if base else "PAID"),
                version_name=version_name,
                lifecycle_state="DRAFT" if draft else None,
            )
            await _apply_grants(created.slug, prepared, {})
            if not draft:
                notes.append(
                    "the version is PUBLISHED; move instances to it with kaiten_update_instance"
                )
            return shapes.report(
                "created",
                "license",
                await _version_view(
                    created, [], list_instances=True, plan=fam.slug, ai_enabled=ai_enabled
                ),
                notes=notes,
            )

        if not name or not type:
            raise ToolError(
                "A new plan needs name and type. To change an existing plan pass license or new_version_of."
            )
        prepared = await _prepare_grants(grants or {}, {}, ai_enabled=ai_enabled, creating=True)
        created = await k.licenses.create(
            name=name,
            type=type,
            description=description or "",
            version_name=version_name,
            slug=shapes.slug(slug, "license") if slug else None,
            lifecycle_state="DRAFT" if draft else None,
        )
        await _apply_grants(created.slug, prepared, {})
        return shapes.report(
            "created",
            "license",
            await _version_view(
                created, [], list_instances=True, plan=created.slug, ai_enabled=ai_enabled
            ),
        )

    @mcp.tool(annotations=WRITE)
    async def kaiten_set_license_state(
        license: Annotated[str, Field(description="License version slug")],
        action: Annotated[
            Literal["publish", "archive", "unarchive", "make_default", "unset_default"],
            Field(
                description=(
                    "publish: put a DRAFT on sale. archive: withdraw a PUBLISHED version from sale (instances already on "
                    "it keep it). unarchive: put it back on sale. make_default / unset_default: pin which version the "
                    "plan serves; without a default it serves its highest published version"
                )
            ),
        ],
    ) -> dict:
        """Move a license version through its lifecycle, or pin it as its plan's default version. Asking for the
        state it is already in reports 'unchanged'."""
        k = sdk.kaiten()
        lslug = shapes.slug(license, "license")
        cur = await sdk.require(k.licenses.get(lslug), f"license {lslug!r}")
        plan = (await views.plan_slugs()).get(cur.family_id)
        target = {"publish": "PUBLISHED", "archive": "ARCHIVED", "unarchive": "PUBLISHED"}.get(
            action
        )
        if (
            (target and cur.lifecycle_state == target)
            or (action == "make_default" and cur.is_default)
            or (action == "unset_default" and not cur.is_default)
        ):
            return shapes.report("unchanged", "license", shapes.license_(cur, plan))
        if target:
            await getattr(k.licenses, action)(lslug)
        else:
            await k.licenses.update(
                lslug,
                name=cur.name,
                type=cur.type,
                description=cur.description,
                version_name=cur.version_name,
                is_default=action == "make_default",
            )
        after = await k.licenses.get(lslug)
        return shapes.report(
            "updated",
            "license",
            shapes.license_(after, plan),
            changes=shapes.changes(shapes.license_(cur, plan), shapes.license_(after, plan)),
        )
