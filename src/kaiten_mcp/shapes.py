# Copyright 2026 KAITEN INC
# SPDX-License-Identifier: Apache-2.0

"""How Kaiten resources are shown to an agent, and how its inputs are read.

The SDK's models carry everything a console needs: ids, audit users,
timestamps. An agent reasons better over less, so every tool projects a model
onto the fields that carry meaning and names it by slug -- the identifier
every tool accepts back. The enforcement arithmetic is the SDK's own
(``kaitencloud.usage``), so a limit reads here exactly as the server applies it.
"""

from __future__ import annotations

import re
from datetime import UTC, date, datetime, timedelta
from typing import Any

from fastmcp.exceptions import ToolError
from kaitencloud import usage as enforcement

SLUG = re.compile(r"^[a-z0-9][a-z0-9-]*[a-z0-9]$")
UNLIMITED = enforcement.UNLIMITED
METERED = ("NUMBER", "NUMBER_AI_CREDIT")


def slug(value: str, what: str = "slug") -> str:
    value = (value or "").strip()
    if not SLUG.match(value) or len(value) > 100:
        raise ToolError(
            f"{what} {value!r} is not a Kaiten slug: lowercase letters, digits and dashes, "
            "2-100 characters, starting and ending with a letter or digit."
        )
    return value


def drop_none(d: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in d.items() if v is not None}


# --- time -------------------------------------------------------------------


def now() -> datetime:
    return datetime.now(UTC)


def rfc3339(t: datetime) -> str:
    return t.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def parse_time(value: str, what: str = "date") -> datetime:
    """Accept '2027-01-31', '2027-01-31T12:00:00Z' or any ISO 8601 timestamp."""
    raw = value.strip()
    try:
        if len(raw) == 10:
            d = date.fromisoformat(raw)
            return datetime(d.year, d.month, d.day, tzinfo=UTC)
        t = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        return t if t.tzinfo else t.replace(tzinfo=UTC)
    except ValueError as e:
        raise ToolError(f"{what} {value!r} is not an ISO 8601 date or timestamp") from e


def as_time(value: datetime | str | None) -> datetime | None:
    if value is None or isinstance(value, datetime):
        return value
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def iso(value: datetime | str | None) -> str | None:
    t = as_time(value)
    return rfc3339(t) if t else None


def days_until(value: datetime | str | None) -> int | None:
    t = as_time(value)
    return None if t is None else (t - now()).days


def plus_days(t: datetime, days: int) -> datetime:
    return t + timedelta(days=days)


# --- values and limits ------------------------------------------------------


def plain(value: Any) -> Any:
    """Unwrap a typed entitlement value ({type, value[, event_count]}) to the value itself."""
    if value is None:
        return None
    if isinstance(value, dict):
        return value.get("value", value)
    return getattr(value, "value", value)


def grant_value(raw: Any) -> Any:
    """Read a grant value an agent wrote: 'unlimited' is the SDK's UNLIMITED sentinel."""
    return UNLIMITED if raw == "unlimited" else raw


def usage_state(
    used: float | None,
    limit: float | None,
    overage_percent: int | None = None,
    warning_percent: int | None = None,
) -> dict[str, Any]:
    """Where a metered usage stands against its grant.

    States: ok; warning (at or past the entitlement's warning threshold);
    at_limit (exactly the included value); overage (past the value, within the
    grant's allowance); over (past what the server accepts); unlimited.
    """
    used = used or 0
    if limit is None:
        return {"used": used, "limit": None, "state": "not_granted"}
    if enforcement.is_unlimited(limit) or overage_percent == UNLIMITED:
        return {"used": used, "limit": "unlimited", "state": "unlimited"}
    out: dict[str, Any] = {"used": used, "limit": limit}
    pct = enforcement.percentage_used(limit, used)
    out["percent"] = round(pct, 1) if pct is not None else None
    if enforcement.is_soft_limit(limit, overage_percent):
        out["hard_ceiling"] = enforcement.maximum_allowed_usage(limit, overage_percent)
    if not enforcement.accepts(limit, used, overage_percent):
        state = "over"
    elif used > limit:
        state = "overage"
    elif used == limit:
        state = "at_limit"
    elif warning_percent and pct is not None and pct >= warning_percent:
        state = "warning"
    else:
        state = "ok"
    out["state"] = state
    return out


ATTENTION_STATES = {"warning", "at_limit", "overage", "over"}


# --- projections ------------------------------------------------------------


def customer(c: Any) -> dict:
    return drop_none(
        {
            "slug": c.slug,
            "name": c.name,
            "domain": c.domain,
            "external_customer_id": c.external_customer_id,
            "integrations": {
                k: getattr(v, "external_id", v) for k, v in (c.integrations or {}).items()
            }
            or None,
        }
    )


def instance(i: Any) -> dict:
    return drop_none(
        {
            "slug": i.slug,
            "name": i.name,
            "description": i.description or None,
            "customer": i.customer_slug,
            "license": i.license_slug,
            "deployment_zone": i.deployment_zone_slug,
            "status": i.status,
            "lifecycle_stage": i.lifecycle_stage,
            "license_start": iso(i.start_license_date),
            "license_end": iso(i.end_license_date),
            "license_days_left": days_until(i.end_license_date),
            "metadata": i.metadata or None,
        }
    )


def license_(lic: Any, plan: str | None = None) -> dict:
    return drop_none(
        {
            "slug": lic.slug,
            "name": lic.name,
            "plan": plan or lic.family_slug,
            "version": lic.version,
            "version_name": lic.version_name,
            "type": lic.type,
            "state": lic.lifecycle_state,
            "is_default": lic.is_default,
            "description": lic.description or None,
        }
    )


def entitlement(e: Any) -> dict:
    return drop_none(
        {
            "slug": e.slug,
            "name": e.name,
            "type": e.type,
            "description": e.description or None,
            "aggregation": e.aggregation_method,
            "reset_period": e.reset_period,
            "reset_anchor": e.reset_anchor,
            "unit": e.unit_plural or e.unit_singular,
            "warning_threshold_percent": e.warning_threshold_percent or None,
            "user_facing": e.user_facing,
            "groups": [g.slug for g in e.entitlement_groups or []] or None,
        }
    )


def grant(g: Any) -> dict:
    value = plain(g.value)
    out: dict[str, Any] = {
        "entitlement": g.entitlement_slug,
        "type": g.entitlement_type,
        "value": value,
    }
    pct = g.limit_cap_exceeded_overage_percent
    if g.entitlement_type in METERED:
        if enforcement.is_unlimited(value):
            out["value"] = "unlimited"
        elif enforcement.is_soft_limit(value, pct):
            out["overage_percent"] = pct
        else:
            out["limit"] = "hard"
    return out


def zone(z: Any) -> dict:
    return drop_none(
        {
            "slug": z.slug,
            "name": z.name,
            "type": z.type,
            "description": z.description or None,
            "metadata": z.metadata or None,
        }
    )


def release(r: Any) -> dict:
    return drop_none(
        {
            "slug": r.slug,
            "version": r.version,
            "description": r.description or None,
            "created_at": iso(r.created_at),
            "components": [f"{c.name}@{c.version}" for c in r.components or []] or None,
        }
    )


# --- write reports ----------------------------------------------------------


def changes(before: dict, after: dict) -> dict[str, dict[str, Any]]:
    """Field-by-field difference between two flat projections."""
    return {
        key: {"from": before.get(key), "to": after.get(key)}
        for key in sorted(set(before) | set(after))
        if before.get(key) != after.get(key)
    }


def merge_map(current: dict | None, patch: dict | None) -> dict:
    """Merge patch into current; a key set to null is removed."""
    merged = dict(current or {})
    for key, value in (patch or {}).items():
        if value is None:
            merged.pop(key, None)
        else:
            merged[key] = value
    return merged


def report(
    status: str,
    kind: str,
    resource: dict | None,
    *,
    changes: dict | None = None,
    notes: list[str] | None = None,
    **extra: Any,
) -> dict:
    """The answer every write gives: what happened, to what, and what changed.

    status is created, updated, unchanged, recorded, deleted, rejected or
    preview. 'unchanged' is a real outcome, not a failure: the resource was
    already in the requested state and nothing was written. 'rejected' means
    Kaiten (or a pre-flight check) refused, and nothing was written either.
    """
    out: dict[str, Any] = {"status": status, "kind": kind}
    if resource is not None:
        out[kind] = resource
    if changes:
        out["changes"] = changes
    out.update(drop_none(extra))
    if notes:
        out["notes"] = notes
    return out
