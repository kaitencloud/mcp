# Copyright 2026 KAITEN INC
# SPDX-License-Identifier: Apache-2.0

"""AI credit control-plane intents, through the SDK's authenticated transport.

No provider calls: models are organization-owned identities, rules belong to
one exact license version, and usage is reported by the caller's application.
"""

from __future__ import annotations

import re
from decimal import Decimal
from typing import Annotated, Any, Literal
from uuid import UUID

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from pydantic import Field

from .. import sdk, shapes
from . import APPEND, READ, WRITE

RequestType = Literal["COMPLETION", "EMBEDDING", "IMAGE_GENERATION", "AUDIO", "VIDEO"]
ModelStatus = Literal["ACTIVE", "DEPRECATED", "ARCHIVED"]
Modality = Literal["TEXT", "IMAGE", "AUDIO", "VIDEO", "EMBEDDING", "MULTIMODAL"]
ChargeOnError = Literal["NEVER", "ALWAYS", "PROVIDER_ERROR_ONLY"]
RATE_FIELDS = {
    "perInputToken",
    "perOutputToken",
    "perCachedInputToken",
    "perCacheWriteToken",
    "perRequest",
    "perSecond",
    "perUnit",
}
COST_FIELDS = RATE_FIELDS - {"perSecond"}
MODEL_FIELDS = (
    "slug",
    "name",
    "vendor",
    "modality",
    "status",
    "description",
    "externalIds",
    "metadata",
    "vendorCosts",
)


def decimal_string(value: Any, what: str, digits: int, scale: int) -> str:
    if not isinstance(value, str) or not re.fullmatch(
        rf"[0-9]{{1,{digits}}}(\.[0-9]{{1,{scale}}})?", value
    ):
        raise ToolError(
            f"{what} must be a non-negative decimal string (up to {digits} digits and {scale} decimal places)."
        )
    return value


def rates_in(rates: dict[str, Any], *, costs: bool = False) -> dict:
    allowed = COST_FIELDS if costs else RATE_FIELDS
    if set(rates) - allowed:
        raise ToolError(f"Unknown rate fields: {sorted(set(rates) - allowed)}")
    return {
        key: None
        if value is None and not costs
        else decimal_string(value, key, 6 if costs else 8, 12 if costs else 10)
        for key, value in rates.items()
    }


def usage_in(usage: dict[str, Any]) -> dict:
    integers = {
        "inputTokens",
        "outputTokens",
        "cacheReadInputTokens",
        "cacheWriteInputTokens",
        "durationMs",
    }
    if set(usage) - integers - {"units", "unitType"}:
        raise ToolError(
            "Unknown AI usage fields; use the documented token, durationMs, units and unitType fields."
        )
    for key in integers & usage.keys():
        value = usage[key]
        if type(value) is not int or not 0 <= value <= 2**63 - 1:
            raise ToolError(f"{key} must be a non-negative int64 integer.")
    if ("units" in usage) != ("unitType" in usage):
        raise ToolError("units and unitType must be supplied together.")
    if "units" in usage:
        decimal_string(usage["units"], "units", 12, 6)
    if "unitType" in usage and usage["unitType"] not in ("image", "second", "frame"):
        raise ToolError("unitType must be image, second or frame.")
    return usage


async def require_ai(entitlement: str) -> None:
    ent = await sdk.require(
        sdk.kaiten().entitlements.get(entitlement), f"entitlement {entitlement!r}"
    )
    if ent.type != "NUMBER_AI_CREDIT":
        raise ToolError(f"{entitlement} is {ent.type}; AI calculation requires NUMBER_AI_CREDIT.")


def register(mcp: FastMCP, *, writes: bool) -> None:
    @mcp.tool(annotations=READ)
    async def kaiten_get_ai_models(
        model: str | None = None,
        status: ModelStatus | None = None,
        cursor: str | None = None,
        limit: Annotated[int, Field(ge=1, le=200)] = 50,
    ) -> dict:
        """Organization-owned AI model identities, not a provider catalogue. A model slug returns its definition;
        otherwise returns one bounded page with the API cursor. Costs remain exact decimal strings."""
        if model:
            return await sdk.require(
                sdk.call_unwrapped("GET", f"models/{shapes.slug(model, 'model')}"),
                f"model {model!r}",
            )
        return await sdk.call_unwrapped(
            "GET",
            "models",
            params=shapes.drop_none({"status": status, "cursor": cursor, "limit": limit}),
        )

    @mcp.tool(annotations=READ)
    async def kaiten_get_ai_rules(
        license: str,
        entitlement: str | None = None,
        status: Literal["ACTIVE", "ARCHIVED"] = "ACTIVE",
    ) -> dict:
        """AI conversion rules for one exact license VERSION slug (never resolve a family to its current version).
        Includes rule ids for editing, authorization selectors, exact rates, caps and error charging policy."""
        path = f"licenses/{shapes.slug(license, 'license')}/conversion-rules"
        params = {"status": status}
        if entitlement:
            params["entitlementSlug"] = shapes.slug(entitlement, "entitlement")
        return {"license": license, "rules": await sdk.call_unwrapped("GET", path, params=params)}

    @mcp.tool(annotations=READ)
    async def kaiten_get_ai_history(
        instance: str,
        entitlement: str,
        open_reservations: bool = False,
        transaction_id: str | None = None,
        status: Literal["RESERVED", "COMMITTED", "RELEASED", "EXPIRED"] | None = None,
        cursor: str | None = None,
        limit: Annotated[int, Field(ge=1, le=500)] = 50,
    ) -> dict:
        """AI credit inference history, or open reservations. Returns exact credit strings and API pagination;
        transaction_id identifies the same call used for reserve/commit. Read usage counters remain numeric."""
        path = f"instances/{shapes.slug(instance, 'instance')}/entitlements/{shapes.slug(entitlement, 'entitlement')}/usage"
        if open_reservations:
            if transaction_id is not None or status is not None or cursor is not None:
                raise ToolError(
                    "Open reservations cannot be combined with history filters or a cursor."
                )
            return await sdk.call_unwrapped("GET", f"{path}/reservations")
        return await sdk.call_unwrapped(
            "GET",
            f"{path}/inferences",
            params=shapes.drop_none(
                {
                    "transactionId": transaction_id,
                    "status": status,
                    "cursor": cursor,
                    "limit": limit,
                }
            ),
        )

    if not writes:
        return

    @mcp.tool(annotations=WRITE)
    async def kaiten_save_ai_model(
        model: str,
        name: Annotated[str | None, Field(min_length=1, max_length=100)] = None,
        vendor: Annotated[str | None, Field(min_length=1, max_length=100)] = None,
        modality: Modality | None = None,
        status: ModelStatus | None = None,
        description: str | None = None,
        external_ids: list[str] | None = None,
        vendor_costs: dict[str, Any] | None = None,
    ) -> dict:
        """Create or merge an organization-owned AI model identity. Creation needs name, vendor and modality.
        external_ids replaces exact provider aliases; vendor_costs replaces USD costs, as decimal strings.
        No credentials or provider calls. ARCHIVED authorizes no usage; DEPRECATED keeps existing rules."""
        mslug = shapes.slug(model, "model")
        if external_ids is not None and (
            any(not s.strip() for s in external_ids) or len(set(external_ids)) != len(external_ids)
        ):
            raise ToolError("external_ids must contain unique, non-empty exact identifiers.")
        costs = None
        if vendor_costs is not None:
            costs = {"currency": "USD", **rates_in(vendor_costs, costs=True)}
        patch = shapes.drop_none(
            {
                "name": name,
                "vendor": vendor,
                "modality": modality,
                "status": status,
                "description": description,
                "externalIds": external_ids,
                "vendorCosts": costs,
            }
        )
        path = f"models/{mslug}"
        cur = await sdk.get_or_none(sdk.call_unwrapped("GET", path))
        if cur is None:
            if not name or not vendor or not modality:
                raise ToolError("A new model requires name, vendor and modality.")
            after = await sdk.call_unwrapped(
                "POST", "models", {"slug": mslug, "status": "ACTIVE", **patch}, idempotent=False
            )
            return shapes.report("created", "model", after)
        wanted = {**{key: cur[key] for key in MODEL_FIELDS if key in cur}, **patch}
        if all(cur.get(key) == value for key, value in patch.items()):
            return shapes.report("unchanged", "model", cur)
        await sdk.call_unwrapped("PUT", path, wanted)
        after = await sdk.call_unwrapped("GET", path)
        return shapes.report("updated", "model", after, changes=shapes.changes(cur, after))

    @mcp.tool(annotations=APPEND)
    async def kaiten_save_ai_rule(
        license: str,
        entitlement: str,
        rates: dict[str, Any],
        rule_id: str | None = None,
        model: str | None = None,
        request_type: RequestType | None = None,
        minimum_credits: str | None = None,
        maximum_credits: str | None = None,
        charge_on_error: ChargeOnError | None = None,
    ) -> dict:
        """Create an AI conversion rule, or edit rule_id on one exact license VERSION. Rates replaces the rate map,
        using exact non-negative decimal strings (8 digits, 10 places). Credit caps use 12 digits, 6 places.
        Omitted fields are kept on edit; creating without model/request_type makes a wildcard selector.
        Creation is not retry-safe: inspect rules after an uncertain create before trying again.
        Changes affect instances on this version immediately; inspect kaiten_get_catalog first.
        Reservations do not lock rates. Use a new version for safe changes; rules are not copied by save_license."""
        path = f"licenses/{shapes.slug(license, 'license')}/conversion-rules"
        eslug = shapes.slug(entitlement, "entitlement")
        given = {"entitlementSlug": eslug, "rates": rates_in(rates)}
        if model is not None:
            given["modelSlug"] = shapes.slug(model, "model")
        if request_type is not None:
            given["requestType"] = request_type
        for key, value in (
            ("minimumCreditsPerRequest", minimum_credits),
            ("maximumCreditsPerRequest", maximum_credits),
        ):
            if value is not None:
                given[key] = decimal_string(value, key, 12, 6)
        if charge_on_error is not None:
            given["chargeOnError"] = charge_on_error
        cur = None
        if rule_id:
            try:
                rule_id = str(UUID(rule_id))
            except ValueError as e:
                raise ToolError("rule_id must be a UUID returned by kaiten_get_ai_rules.") from e
            path += f"/{rule_id}"
            cur = await sdk.require(sdk.call_unwrapped("GET", path), f"conversion rule {rule_id!r}")
            if cur["status"] != "ACTIVE":
                raise ToolError("Archived conversion rules cannot be edited; create a new rule.")
            keys = (
                "entitlementSlug",
                "modelSlug",
                "requestType",
                "rates",
                "minimumCreditsPerRequest",
                "maximumCreditsPerRequest",
                "chargeOnError",
            )
            given = {**{key: cur[key] for key in keys}, **given}
        minimum = given.get("minimumCreditsPerRequest", "0")
        maximum = given.get("maximumCreditsPerRequest")
        if maximum is not None and Decimal(minimum) > Decimal(maximum):
            raise ToolError("minimum_credits must not exceed maximum_credits.")
        await require_ai(eslug)
        if cur and all(cur.get(key) == value for key, value in given.items()):
            return shapes.report("unchanged", "rule", cur)
        after = await sdk.call_unwrapped(
            "PUT" if cur else "POST", path, given, idempotent=bool(cur)
        )
        return shapes.report(
            "updated" if cur else "created",
            "rule",
            after,
            changes=shapes.changes(cur, after) if cur else None,
            notes=[
                f"Scoped to license version {license}; instances on it see this rule immediately."
            ],
        )

    @mcp.tool(annotations=WRITE)
    async def kaiten_report_ai_usage(
        instance: str,
        entitlement: str,
        transaction_id: Annotated[
            str, Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9._:-]+$")
        ],
        phase: Literal["reserve", "commit"],
        usage: dict[str, Any],
        model: Annotated[str | None, Field(min_length=1, max_length=255)] = None,
        request_type: RequestType | None = None,
        request_status: Literal["SUCCESS", "PROVIDER_ERROR", "CUSTOMER_ERROR", "NOT_SENT"]
        | None = None,
        ttl_seconds: Annotated[int | None, Field(ge=1, le=2**63 - 1)] = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict:
        """Account caller-reported AI usage; never executes inference. Reserve estimates/holds credits; commit
        reports actual quantities, or NOT_SENT releases the reservation. Commit requires request_status;
        model/request_type may be omitted to inherit a reservation. Inspect usage.ai authorization/warnings.
        Use the same transaction_id for both phases, scoped to instance + entitlement. Retry only an identical
        phase/body; never supply Idempotency-Key. Commits may account beyond limits. Credits remain strings."""
        islug, eslug = shapes.slug(instance, "instance"), shapes.slug(entitlement, "entitlement")
        if not transaction_id.strip():
            raise ToolError("transaction_id cannot be blank.")
        quantities = usage_in(usage)
        if model is not None and "\u0000" in model:
            raise ToolError("model must not contain a NUL character.")
        if phase == "reserve" and (
            model is None or request_type is None or request_status is not None
        ):
            raise ToolError("Reserve requires model and request_type, and refuses request_status.")
        if phase == "commit" and (request_status is None or ttl_seconds is not None):
            raise ToolError("Commit requires request_status and must omit ttl_seconds.")
        value = shapes.drop_none(
            {
                "type": "ai_usage",
                "phase": phase,
                "transactionId": transaction_id,
                "model": model,
                "requestType": request_type,
                "usage": quantities,
                "requestStatus": request_status,
                "ttlSeconds": ttl_seconds,
            }
        )
        await require_ai(eslug)
        after = await sdk.call_unwrapped(
            "POST",
            f"instances/{islug}/entitlements/{eslug}/usage",
            shapes.drop_none({"value": value, "metadata": metadata}),
            idempotent=True,
        )
        return shapes.report("recorded", "usage", after)
