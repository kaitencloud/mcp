# Copyright 2026 KAITEN INC
# SPDX-License-Identifier: Apache-2.0

"""Composite reads: the views several tools share, assembled from SDK calls.

The questions an agent asks ("how is Sakura Tokyo doing", "who is near their limit")
span a customer, its instances, their license grants and their usage. Each is
one SDK call; a view fetches them concurrently, fetches each license's grants
once however many instances share it, and only fetches usage when asked.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from typing import Any

from fastmcp.exceptions import ToolError

from . import ai_policy, sdk, shapes

# Concurrent requests a single tool call may have in flight.
FANOUT = 8


async def gather_bounded[T](calls: Iterable[Awaitable[T]]) -> list[T]:
    gate = asyncio.Semaphore(FANOUT)

    async def run(call: Awaitable[T]) -> T:
        async with gate:
            return await call

    return await asyncio.gather(*(run(c) for c in calls))


@dataclass
class Snapshot:
    """Instances with everything needed to judge them, keyed by slug."""

    instances: list[Any]
    licenses: dict[str, Any]
    grants: dict[str, list[Any]]
    entitlements: dict[str, Any]
    plans: dict[str, str]  # license family id -> plan slug
    usage: dict[str, list[Any]] = field(default_factory=dict)
    ai_enabled: bool = False

    def license_of(self, inst: Any) -> Any:
        return self.licenses.get(inst.license_slug)

    def plan_of(self, inst: Any) -> str | None:
        lic = self.license_of(inst)
        return self.plans.get(lic.family_id) if lic else None


async def snapshot(
    instances: list[Any] | None = None,
    *,
    keep: Callable[[Any], bool] | None = None,
    with_usage: bool = True,
    ai_enabled: bool = False,
) -> Snapshot:
    k = sdk.kaiten()
    if instances is None:
        instances = await k.instances.list()
    licenses, entitlements, plans = await asyncio.gather(
        k.licenses.list(), k.entitlements.list(), plan_slugs()
    )
    snap = Snapshot(
        instances=[i for i in instances if keep is None or keep(i)],
        licenses={lic.slug: lic for lic in licenses},
        grants={},
        entitlements={e.slug: e for e in entitlements},
        plans=plans,
        ai_enabled=ai_enabled,
    )
    used = sorted({i.license_slug for i in snap.instances})
    for slug_, grants in zip(
        used, await gather_bounded(k.licenses.list_entitlements(s) for s in used), strict=True
    ):
        snap.grants[slug_] = grants
    if with_usage:
        rows = await gather_bounded(k.instances.list_usage(i.slug) for i in snap.instances)
        snap.usage = {i.slug: u for i, u in zip(snap.instances, rows, strict=True)}
    return snap


def entitlements_view(snap: Snapshot, inst: Any) -> dict[str, Any]:
    """Split an instance's grants into limits (with usage), features and config."""
    usage = {u.entitlement_slug: u for u in snap.usage.get(inst.slug, [])}
    limits, features, config = [], {}, {}
    for g in snap.grants.get(inst.license_slug, []):
        slug_ = g.entitlement_slug
        definition = snap.entitlements.get(slug_)
        if not ai_policy.visible(g, enabled=snap.ai_enabled, definition=definition):
            continue
        if g.entitlement_type in shapes.METERED:
            u = usage.get(slug_)
            row = {
                "entitlement": slug_,
                **shapes.usage_state(
                    shapes.plain(u.value) if u else 0,
                    shapes.plain(g.value),
                    g.limit_cap_exceeded_overage_percent,
                    definition.warning_threshold_percent if definition else None,
                ),
            }
            if definition and (definition.unit_plural or definition.unit_singular):
                row["unit"] = definition.unit_plural or definition.unit_singular
            if u and u.current_period_end:
                row["resets_at"] = shapes.iso(u.current_period_end)
            limits.append(row)
        elif g.entitlement_type == "BOOLEAN":
            features[slug_] = shapes.plain(g.value)
        elif g.entitlement_type == "CONFIG":
            config[slug_] = shapes.plain(g.value)
    return {"limits": limits, "features": features, "config": config}


def instance_summary(
    snap: Snapshot, inst: Any, *, with_entitlements: bool = True
) -> dict[str, Any]:
    out = shapes.instance(inst)
    lic = snap.license_of(inst)
    if lic:
        out["plan"] = snap.plans.get(lic.family_id)
        out["license_version"] = lic.version
    if with_entitlements:
        out.update(entitlements_view(snap, inst))
    return out


def attention(snap: Snapshot, inst: Any, *, expiring_within_days: int = 30) -> list[str]:
    """Why this instance deserves a look, in plain words; empty when it does not."""
    reasons = []
    if inst.status and inst.status != "HEALTHY":
        reasons.append(f"status is {inst.status}")
    left = shapes.days_until(inst.end_license_date)
    if left is not None and left < 0:
        reasons.append(f"license ended {-left} days ago")
    elif left is not None and left <= expiring_within_days:
        reasons.append(f"license ends in {left} days")
    lic = snap.license_of(inst)
    if lic and lic.lifecycle_state == "ARCHIVED":
        reasons.append(f"on archived license version {lic.slug}")
    if snap.usage:
        for row in entitlements_view(snap, inst)["limits"]:
            if row["state"] in shapes.ATTENTION_STATES:
                pct = f" ({row['percent']}%)" if row.get("percent") is not None else ""
                reasons.append(
                    f"{row['entitlement']} {row['state']}: {row['used']}/{row['limit']}{pct}"
                )
    return reasons


# --- resolution -------------------------------------------------------------


async def plan_slugs() -> dict[str, str]:
    """License family id -> plan slug. Licenses name their family by id only."""
    return {f.id: f.slug for f in await sdk.kaiten().license_families.list()}


async def instance(slug_: str) -> Any:
    return await sdk.require(
        sdk.kaiten().instances.get(shapes.slug(slug_, "instance")), f"instance {slug_!r}"
    )


async def resolve_license(*, plan: str | None, license: str | None) -> Any:
    """A plan is a license family and resolves to its current version; a license is one exact version."""
    k = sdk.kaiten()
    if plan and license:
        raise ToolError(
            "Pass either plan (a family, resolved to its current version) or license (an exact version), not both."
        )
    if license:
        return await sdk.require(
            k.licenses.get(shapes.slug(license, "license")), f"license version {license!r}"
        )
    if not plan:
        raise ToolError(
            "A plan (license family slug) or a license (exact version slug) is required."
        )
    family = await sdk.require(k.license_families.get(shapes.slug(plan, "plan")), f"plan {plan!r}")
    if not family.current_version:
        raise ToolError(
            f"Plan {plan!r} has no servable version: every version is a draft or archived. "
            "Publish one with kaiten_set_license_state, or pass an exact license."
        )
    return family.current_version


async def releases() -> list[Any]:
    """Every release with its components: the list endpoint carries none, so each is read on its own."""
    k = sdk.kaiten()
    return await gather_bounded(k.releases.get(r.slug) for r in await k.releases.list())
