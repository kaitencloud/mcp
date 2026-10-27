# Copyright 2026 KAITEN INC
# SPDX-License-Identifier: Apache-2.0

"""Usage metering: reporting consumption, and reading it against the limits."""

from __future__ import annotations

from typing import Annotated, Any, Literal

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from kaitencloud import ThresholdExceededError
from kaitencloud.types import UsageBehavior
from pydantic import Field

from .. import ai_policy, sdk, shapes, views
from . import APPEND, READ

State = Literal["ok", "warning", "at_limit", "overage", "over", "unlimited"]


def register(mcp: FastMCP, *, writes: bool, ai_enabled: bool = False) -> None:
    @mcp.tool(annotations=READ)
    async def kaiten_get_usage(
        entitlement: Annotated[
            str | None, Field(description="Only this entitlement (slug)")
        ] = None,
        customer: Annotated[str | None, Field(description="Only this customer's instances")] = None,
        plan: Annotated[
            str | None, Field(description="Only instances on this plan (license family slug)")
        ] = None,
        min_percent: Annotated[
            float | None, Field(description="Only rows at or above this percentage of their limit")
        ] = None,
        states: Annotated[
            list[State] | None, Field(description="Only rows in these states")
        ] = None,
        limit: Annotated[int, Field(ge=1, le=500)] = 50,
    ) -> dict:
        """Usage of metered NUMBER entitlements across instances, highest percentage first: who is
        close to a limit, who is in overage, how much of a quota a customer has left. States: ok, warning (past the
        entitlement's warning threshold), at_limit, overage (past the included value but within the grant's
        overage allowance), over, unlimited."""
        k = sdk.kaiten()
        snap = await views.snapshot(
            await k.instances.list(),
            keep=lambda i: not customer or i.customer_slug == customer,
            ai_enabled=ai_enabled,
        )
        if entitlement:
            eslug = shapes.slug(entitlement, "entitlement")
            ai_policy.require_available(snap.entitlements.get(eslug), enabled=ai_enabled)
            for grants in snap.grants.values():
                for grant in grants:
                    if grant.entitlement_slug == eslug:
                        ai_policy.require_available(grant, enabled=ai_enabled)
        rows = []
        for i in snap.instances:
            if plan and snap.plan_of(i) != plan:
                continue
            for row in views.entitlements_view(snap, i)["limits"]:
                if entitlement and row["entitlement"] != entitlement:
                    continue
                if states and row["state"] not in states:
                    continue
                if min_percent is not None and (
                    row.get("percent") is None or row["percent"] < min_percent
                ):
                    continue
                rows.append(
                    {
                        "instance": i.slug,
                        "customer": i.customer_slug,
                        "license": i.license_slug,
                        **row,
                    }
                )
        rows.sort(key=lambda r: -(r.get("percent") or 0))
        return shapes.drop_none(
            {"total": len(rows), "usage": rows[:limit], "truncated": len(rows) > limit or None}
        )

    if not writes:
        return

    @mcp.tool(annotations=APPEND)
    async def kaiten_report_usage(
        instance: Annotated[str, Field(description="Instance slug")],
        entitlement: Annotated[str, Field(description="Metered entitlement slug")],
        amount: Annotated[float, Field(description="Quantity to report")],
        behavior: Annotated[
            UsageBehavior,
            Field(
                description="append: an event of this size, aggregated by the entitlement's method (SUM, COUNT, LATEST...). set: replace the current value"
            ),
        ],
        metadata: Annotated[
            dict[str, Any] | None, Field(description="Context recorded with the report")
        ] = None,
    ) -> dict:
        """Report consumption of a metered entitlement for an instance, and say where it now stands against its limit.
        A report the grant refuses (past a hard limit, or past the overage allowance) comes back with status
        'rejected' and is not recorded. Each call is a separate report: never repeat one that answered."""
        k = sdk.kaiten()
        inst = await views.instance(instance)
        eslug = shapes.slug(entitlement, "entitlement")
        snap = await views.snapshot([inst], ai_enabled=ai_enabled)
        grant = next(
            (g for g in snap.grants.get(inst.license_slug, []) if g.entitlement_slug == eslug), None
        )
        if grant is None:
            raise ToolError(
                f"License {inst.license_slug} of instance {inst.slug} does not grant {eslug!r}; nothing to report against."
            )
        ai_policy.require_available(
            grant, enabled=ai_enabled, definition=snap.entitlements.get(eslug)
        )
        if grant.entitlement_type not in shapes.METERED:
            raise ToolError(
                f"{eslug} is a {grant.entitlement_type} entitlement: only NUMBER and NUMBER_AI_CREDIT are metered."
            )
        if ai_policy.is_ai(grant, snap.entitlements.get(eslug)) and behavior != "append":
            raise ToolError(
                "AI credit adjustments accept only append; use kaiten_report_ai_usage for model quantities."
            )
        limit = shapes.plain(grant.value)
        pct = grant.limit_cap_exceeded_overage_percent
        definition = snap.entitlements.get(eslug)
        warn = definition.warning_threshold_percent if definition else None
        prior = next(
            (u for u in snap.usage.get(inst.slug, []) if u.entitlement_slug == eslug), None
        )
        before = shapes.usage_state(shapes.plain(prior.value) if prior else 0, limit, pct, warn)
        try:
            after_raw = await k.instances.report_usage(
                inst.slug, eslug, amount, behavior=behavior, metadata=metadata
            )
        except ThresholdExceededError as e:
            return shapes.report(
                "rejected",
                "usage",
                {"instance": inst.slug, "entitlement": eslug, **before},
                reason=f"{e.code}: {e.detail}",
                notes=[
                    "The report was refused and not recorded. Raise the grant, or move the instance to a bigger plan."
                ],
            )
        after = shapes.usage_state(shapes.plain(after_raw.value), limit, pct, warn)
        notes = (
            [f"crossed from {before['state']} to {after['state']}"]
            if after["state"] != before["state"]
            else []
        )
        return shapes.report(
            "recorded",
            "usage",
            {"instance": inst.slug, "entitlement": eslug, **after},
            before=before.get("used"),
            resets_at=shapes.iso(after_raw.current_period_end),
            notes=notes,
        )
