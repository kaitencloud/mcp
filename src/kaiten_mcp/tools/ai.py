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
from kaitencloud import NotFoundError
from pydantic import Field

from .. import ai_pricing, sdk, shapes
from . import APPEND, DESTRUCTIVE, READ, WRITE

RequestType = Literal["COMPLETION", "EMBEDDING", "IMAGE_GENERATION", "AUDIO", "VIDEO"]
ModelStatus = Literal["ACTIVE", "DEPRECATED", "ARCHIVED"]
Modality = Literal["TEXT", "IMAGE", "AUDIO", "VIDEO", "EMBEDDING", "MULTIMODAL"]
ChargeOnError = Literal["NEVER", "ALWAYS", "PROVIDER_ERROR_ONLY"]
InferenceStatus = Literal["RESERVED", "COMMITTED", "RELEASED", "EXPIRED"]
TransactionId = Annotated[str, Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9._:-]+$")]
# A lookup by transaction id reaches back this far: the listings otherwise default to 30 days.
BEGINNING = "1970-01-01T00:00:00Z"
# How many operations the unpriced-usage summary reads at most, and per page.
SCAN_MAX, PAGE = 5000, 500
# How many catalogue models the rate card reads at most to resolve an external id.
MODELS_MAX = 2000
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


def pool_path(instance: str, entitlement: str) -> str:
    return (
        f"instances/{shapes.slug(instance, 'instance')}"
        f"/entitlements/{shapes.slug(entitlement, 'entitlement')}/usage"
    )


def history_params(
    *,
    since: str | None,
    until: str | None,
    transaction_id: str | None,
    status: str | None,
    authorized: bool | None,
    model_slug: str | None,
    model: str | None,
    request_type: str | None,
) -> dict:
    """The listings' filters. A transaction id is looked up over the whole history, not the
    listings' default 30 days, unless since says otherwise."""
    return shapes.drop_none(
        {
            "from": shapes.rfc3339(shapes.parse_time(since, "since"))
            if since
            else (BEGINNING if transaction_id else None),
            "to": shapes.rfc3339(shapes.parse_time(until, "until")) if until else None,
            "transactionId": transaction_id,
            "status": status,
            "authorized": None if authorized is None else str(authorized).lower(),
            "modelSlug": shapes.slug(model_slug, "model") if model_slug else None,
            "model": model,
            "requestType": request_type,
        }
    )


async def inference(instance: str, entitlement: str, transaction_id: str) -> dict:
    """One inference row, by its transaction id, over the pool's whole history."""
    page = await sdk.require(
        sdk.call_unwrapped(
            "GET",
            f"{pool_path(instance, entitlement)}/inferences",
            params={"transactionId": transaction_id, "from": BEGINNING, "limit": 1},
        ),
        f"AI credit pool {entitlement!r} on instance {instance!r}",
    )
    items = (page or {}).get("items") or []
    if not items:
        raise ToolError(
            f"No AI operation {transaction_id!r} on {instance}/{entitlement}. "
            "Check the transaction id, or find it with kaiten_list_ai_inferences."
        )
    return items[0]


async def catalogue_model(sent: str) -> tuple[dict | None, list[str]]:
    """The catalogue model a call naming `sent` resolves to: by slug, else by exact external id."""
    notes: list[str] = []
    if shapes.SLUG.match(sent) and len(sent) <= 100:
        found = await sdk.get_or_none(sdk.call_unwrapped("GET", f"models/{sent}"))
        if found is not None:
            return found, notes
    models: list[dict] = []
    cursor = None
    while len(models) < MODELS_MAX:
        page = await sdk.call_unwrapped(
            "GET", "models", params=shapes.drop_none({"limit": 200, "cursor": cursor})
        )
        if isinstance(page, list):  # a deployment older than cursor pagination
            models.extend(page)
            break
        models.extend(page.get("items") or [])
        cursor = page.get("nextCursor")
        if not page.get("hasMore") or not cursor:
            break
    else:
        notes.append(f"Only the first {MODELS_MAX} catalogue models were searched for {sent!r}.")
    return ai_pricing.find_model(models, sent), notes


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
        Includes rule ids for editing, authorization selectors, exact rates, caps and error charging policy, who
        created and last changed each rule (createdBy, updatedBy), and whether the licence (grantedByLicense) or
        a compatible add-on (grantedByAddons) grants the pool. For which rule prices a given model, use
        kaiten_get_ai_rate_card."""
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
        status: InferenceStatus | None = None,
        authorized: bool | None = None,
        model_slug: str | None = None,
        model: Annotated[str | None, Field(min_length=1, max_length=255)] = None,
        request_type: RequestType | None = None,
        since: Annotated[str | None, Field(description="ISO date or timestamp")] = None,
        until: Annotated[str | None, Field(description="ISO date or timestamp")] = None,
        cursor: str | None = None,
        limit: Annotated[int, Field(ge=1, le=500)] = 50,
    ) -> dict:
        """AI credit inference history of one pool on one instance, or its open reservations. One bounded page
        with the API cursor; credits stay exact strings. Each row carries, where the server keeps them: the
        pricing snapshot that charged it (pricing: rule, specificity, rates, caps, error policy), the licence
        whose rules were considered, warnings, reportSeq (the usage journal link), vendorCost {reported,
        computed}, refunds and credits.refunded. Filters: authorized (false = usage no rule priced), model_slug
        (the catalogue model), model (exactly as sent; finds models the catalogue does not know),
        request_type, status, since/until (default: the last 30 days; a transaction_id alone searches the whole
        history). Releases that closed no reservation are listed only with status=RELEASED or a transaction_id.
        To explain one charge, use kaiten_explain_ai_charge. Read usage counters remain numeric."""
        path = pool_path(instance, entitlement)
        if open_reservations:
            if any(
                v is not None
                for v in (
                    transaction_id,
                    status,
                    authorized,
                    model_slug,
                    model,
                    request_type,
                    since,
                    until,
                    cursor,
                )
            ):
                raise ToolError(
                    "Open reservations cannot be combined with history filters or a cursor."
                )
            return await sdk.call_unwrapped("GET", f"{path}/reservations")
        params = history_params(
            since=since,
            until=until,
            transaction_id=transaction_id,
            status=status,
            authorized=authorized,
            model_slug=model_slug,
            model=model,
            request_type=request_type,
        )
        return await sdk.call_unwrapped(
            "GET",
            f"{path}/inferences",
            params=shapes.drop_none({**params, "cursor": cursor, "limit": limit}),
        )

    @mcp.tool(annotations=READ)
    async def kaiten_list_ai_inferences(
        instance: str | None = None,
        entitlement: str | None = None,
        status: InferenceStatus | None = None,
        transaction_id: str | None = None,
        authorized: bool | None = None,
        model_slug: str | None = None,
        model: Annotated[str | None, Field(min_length=1, max_length=255)] = None,
        request_type: RequestType | None = None,
        since: Annotated[str | None, Field(description="ISO date or timestamp")] = None,
        until: Annotated[str | None, Field(description="ISO date or timestamp")] = None,
        unpriced: Annotated[
            bool,
            Field(
                description="true: summarize the usage no rule priced (authorized=false), grouped by "
                "model as sent and denial reason, instead of returning a page"
            ),
        ] = False,
        scan_limit: Annotated[
            int,
            Field(
                ge=1, le=SCAN_MAX, description="With unpriced: how many operations to read at most"
            ),
        ] = 1000,
        cursor: str | None = None,
        limit: Annotated[int, Field(ge=1, le=500)] = 50,
    ) -> dict:
        """AI operations across the whole organization, newest first: every instance and credit pool, each row
        naming its instance and pool, with the same fields and filters as kaiten_get_ai_history. One bounded page
        with the API cursor. Range: since/until, default the last 30 days.
        unpriced=true answers "which models are customers calling that we do not price?": it reads up to
        scan_limit unauthorized operations (pages of 500) and groups them by model as sent and denial reason
        (UNKNOWN_MODEL, MODEL_ARCHIVED, NO_RULE), with counts, instances, pools, request types and first/last seen.
        The grouping is done here, from the listing; when more operations exist past scan_limit it says so.
        An operation still RESERVED is neither authorized nor unauthorized."""
        params = history_params(
            since=since,
            until=until,
            transaction_id=transaction_id,
            status=status,
            authorized=authorized,
            model_slug=model_slug,
            model=model,
            request_type=request_type,
        )
        if instance:
            params["instanceSlug"] = shapes.slug(instance, "instance")
        if entitlement:
            params["entitlementSlug"] = shapes.slug(entitlement, "entitlement")

        async def page(extra: dict) -> dict:
            try:
                return await sdk.call_unwrapped(
                    "GET", "usage/inferences", params={**params, **extra}
                )
            except NotFoundError as e:
                raise ToolError(
                    "This Kaiten does not serve the organization's AI inference listing "
                    f"(GET /usage/inferences: {e.detail or 'not found'}). Read one pool at a time "
                    "with kaiten_get_ai_history."
                ) from e

        if not unpriced:
            return await page(shapes.drop_none({"cursor": cursor, "limit": limit}))
        if authorized is True or cursor is not None:
            raise ToolError(
                "unpriced reads authorized=false from the start: drop authorized and cursor."
            )
        params["authorized"] = "false"
        groups: dict[tuple, dict] = {}
        scanned, ignored, more, next_cursor = 0, 0, False, None
        while scanned < scan_limit:
            answer = await page(
                shapes.drop_none({"cursor": next_cursor, "limit": min(PAGE, scan_limit - scanned)})
            )
            items = answer.get("items") or []
            for row in items:
                if row.get("authorized") is not False:
                    # A server that does not know the filter lists everything: keep only what it refused.
                    ignored += 1
                    continue
                key = (row.get("model"), row.get("denialReason"))
                g = groups.setdefault(
                    key,
                    {
                        "model": row.get("model"),
                        "denial_reason": row.get("denialReason"),
                        "meaning": ai_pricing.DENIAL_MEANING.get(row.get("denialReason") or ""),
                        "operations": 0,
                        "catalogue_models": set(),
                        "instances": set(),
                        "pools": set(),
                        "request_types": set(),
                        "first_seen": row.get("reportedAt"),
                        "last_seen": row.get("reportedAt"),
                    },
                )
                g["operations"] += 1
                for field, value in (
                    ("catalogue_models", row.get("modelSlug")),
                    ("instances", row.get("instanceSlug")),
                    ("pools", row.get("entitlementSlug")),
                    ("request_types", row.get("requestType")),
                ):
                    if value:
                        g[field].add(value)
                seen = row.get("reportedAt")
                if seen and (not g["first_seen"] or seen < g["first_seen"]):
                    g["first_seen"] = seen
                if seen and (not g["last_seen"] or seen > g["last_seen"]):
                    g["last_seen"] = seen
            scanned += len(items)
            more = bool(answer.get("hasMore"))
            next_cursor = answer.get("nextCursor")
            if not more or not next_cursor or not items:
                break
        rows = sorted(groups.values(), key=lambda g: (-g["operations"], str(g["model"])))
        for g in rows:
            for field in ("catalogue_models", "instances", "pools", "request_types"):
                g[field] = sorted(g[field])
        notes = [
            "Grouped here from the organization listing (authorized=false), not by Kaiten.",
            "Price a model with kaiten_save_ai_model (its identity or external id) and kaiten_save_ai_rule; "
            "check the result with kaiten_get_ai_rate_card. Past usage is not repriced.",
        ]
        if ignored:
            notes.insert(
                0,
                f"{ignored} operations read were not refused ones and were left out: this Kaiten "
                "seems not to filter by authorized, so the summary covers fewer operations than scanned.",
            )
        if more:
            notes.insert(
                0,
                f"Truncated: {scanned} operations read and more exist. Narrow since/until, instance or "
                "entitlement, or raise scan_limit.",
            )
        return shapes.drop_none(
            {
                "scanned": scanned,
                "truncated": more or None,
                "groups": [{k: v for k, v in g.items() if v not in (None, [])} for g in rows],
                "notes": notes,
            }
        )

    @mcp.tool(annotations=READ)
    async def kaiten_explain_ai_charge(
        instance: str,
        entitlement: str,
        transaction_id: TransactionId,
    ) -> dict:
        """Explain one AI operation's charge from its own record: the rule snapshot that priced it (rule id, how it
        matched, rates, minimum and maximum, error policy), the arithmetic recomputed from that snapshot and the
        usage it stored (rate x quantity lines, the error policy, the clamp), set against Kaiten's computed and
        charged credits, the pool counter before and after, vendor cost, warnings with their meaning, refunds and
        what is left to refund. Today's rules are never read: they may have changed since. A row with no snapshot
        says so instead of guessing; Kaiten's stored credits are always the authority."""
        row = await inference(instance, entitlement, transaction_id)
        return {
            "instance": instance,
            "entitlement": entitlement,
            **ai_pricing.explain(row),
        }

    @mcp.tool(annotations=READ)
    async def kaiten_get_ai_rate_card(
        license: str,
        entitlement: str,
        model: Annotated[
            str | None,
            Field(
                min_length=1,
                max_length=255,
                description="A model as an application sends it (catalogue slug or exact external id)",
            ),
        ] = None,
        request_type: RequestType | None = None,
    ) -> dict:
        """The rate card of one credit pool on one exact license VERSION: its active rules, each with its ladder
        level (EXACT_TYPED: the model's own rule for a request type; EXACT_ALL: the model's own rule for every
        type; ANY_TYPED: any model for a type; ANY_ALL: any model, any type), best first. With a model, says
        which rule would price it, for request_type or for every request type: the model as sent resolves to a
        catalogue model by slug or exact external id; unknown and ARCHIVED models are never priced, not even by a
        wildcard. This resolution is computed here from the rules and models read now, with the server's
        ladder: Kaiten decides at report time. No credits are held: never reserve to test a price."""
        lslug, eslug = shapes.slug(license, "license"), shapes.slug(entitlement, "entitlement")
        rules = await sdk.call_unwrapped(
            "GET",
            f"licenses/{lslug}/conversion-rules",
            params={"status": "ACTIVE", "entitlementSlug": eslug},
        )
        rules = [r for r in rules or [] if isinstance(r, dict)]
        order = {lvl: i for i, lvl in enumerate(ai_pricing.LEVELS)}
        card = sorted(
            rules,
            key=lambda r: (
                order[ai_pricing.level(r)],
                r.get("modelSlug") or "",
                r.get("requestType") or "",
            ),
        )
        out: dict[str, Any] = {
            "license": lslug,
            "entitlement": eslug,
            "rules": [
                shapes.drop_none(
                    {
                        "id": r.get("id"),
                        "level": ai_pricing.level(r),
                        "model": r.get("modelSlug") or "any",
                        "request_type": r.get("requestType") or "any",
                        "rates": r.get("rates"),
                        "minimum": r.get("minimumCreditsPerRequest"),
                        "maximum": r.get("maximumCreditsPerRequest"),
                        "charge_on_error": r.get("chargeOnError"),
                        "granted_by_license": r.get("grantedByLicense"),
                        "granted_by_addons": r.get("grantedByAddons"),
                        "updated_by": (r.get("updatedBy") or {}).get("name"),
                        "updated_at": r.get("updatedAt"),
                    }
                )
                for r in card
            ],
        }
        notes: list[str] = []
        if any(r.get("modelSlug") is None for r in rules):
            notes.append(
                "A wildcard rule (model any) prices every catalogue model this pool has no own rule for, "
                "including models added to the catalogue later. Unknown and archived models stay unpriced."
            )
        if rules and not any(r.get("grantedByLicense") or r.get("grantedByAddons") for r in rules):
            notes.append(
                "Neither this licence nor a compatible add-on grants the pool: these rules price nothing "
                "until an instance holds it."
            )
        if not rules:
            notes.append(
                f"No active rule: on {lslug}, every call to {eslug} is unpriced (NO_RULE)."
            )
        if model is not None:
            found, model_notes = await catalogue_model(model)
            notes.extend(model_notes)
            types = [request_type] if request_type else list(ai_pricing.REQUEST_TYPES)
            out["resolution"] = {
                "model_as_sent": model,
                "catalogue_model": shapes.drop_none(
                    {"slug": found.get("slug"), "status": found.get("status")}
                )
                if found
                else None,
                "by_request_type": {t: ai_pricing.resolve(rules, found, t) for t in types},
                "computed": ai_pricing.CLIENT_SIDE,
            }
        if notes:
            out["notes"] = notes
        return out

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
        transaction_id: TransactionId,
        phase: Literal["reserve", "commit"],
        usage: dict[str, Any],
        model: Annotated[str | None, Field(min_length=1, max_length=255)] = None,
        request_type: RequestType | None = None,
        request_status: Literal["SUCCESS", "PROVIDER_ERROR", "CUSTOMER_ERROR", "NOT_SENT"]
        | None = None,
        ttl_seconds: Annotated[int | None, Field(ge=1, le=2**63 - 1)] = None,
        vendor_cost: Annotated[
            str | None,
            Field(
                description="Commit only, not with NOT_SENT: what the provider charged, USD decimal string "
                "(12 digits, 12 places). Evidence only; it never changes the credits charged"
            ),
        ] = None,
        metadata: dict[str, Any] | None = None,
        confirm: Annotated[
            bool,
            Field(
                description="false (default): only show the report that would be sent. true: send it, "
                "which holds (reserve) or charges (commit) the customer's credits"
            ),
        ] = False,
    ) -> dict:
        """Account caller-reported AI usage against a customer's credit pool; never executes inference. A reserve
        HOLDS credits for up to ttl_seconds and a commit CHARGES them, so call it first without confirm to see the
        report, then with confirm=true. Never reserve to test a price: use kaiten_get_ai_rate_card.
        Commit reports actual quantities, or NOT_SENT releases the reservation (a release with no reservation
        closes the transaction id). Commit requires request_status; model/request_type may be omitted to inherit
        a reservation. Use the same transaction_id for both phases, scoped to instance + entitlement. Retry only an
        identical phase/body (vendor_cost byte for byte: "0.01" is not "0.010"); never supply Idempotency-Key.
        Commits may account beyond limits. Inspect usage.ai: status, authorization, rule {id, specificity},
        warnings, exact credit strings and the remaining balance."""
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
        if vendor_cost is not None:
            if phase != "commit" or request_status == "NOT_SENT":
                raise ToolError(
                    "vendor_cost is accepted on a commit of a sent request only, not on reserve or NOT_SENT."
                )
            decimal_string(vendor_cost, "vendor_cost", 12, 12)
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
                "vendorCost": vendor_cost,
            }
        )
        await require_ai(eslug)
        body = shapes.drop_none({"value": value, "metadata": metadata})
        if not confirm:
            effect = (
                "holds the estimate's credits until commit, release or expiry"
                if phase == "reserve"
                else (
                    "releases the reservation without charging"
                    if request_status == "NOT_SENT"
                    else "charges the customer's pool for this usage"
                )
            )
            return shapes.report(
                "preview",
                "usage",
                {"instance": islug, "entitlement": eslug, "request": body},
                notes=[
                    f"Nothing was sent. This {phase} {effect}. Call again with confirm=true to send it.",
                    "To see which rule would price it, use kaiten_get_ai_rate_card.",
                ],
            )
        after, headers = await sdk.call_unwrapped_with_headers(
            "POST",
            f"instances/{islug}/entitlements/{eslug}/usage",
            body,
            idempotent=True,
        )
        replayed = (headers.get("idempotent-replayed") or "").lower() == "true"
        return shapes.report(
            "recorded",
            "usage",
            after,
            replayed=True if replayed else None,
            notes=["A replay of an earlier identical report: nothing was counted again."]
            if replayed
            else None,
        )

    @mcp.tool(annotations=DESTRUCTIVE)
    async def kaiten_refund_ai_inference(
        instance: str,
        entitlement: str,
        transaction_id: TransactionId,
        idempotency_key: Annotated[
            str,
            Field(
                min_length=1,
                max_length=128,
                pattern=r"^[A-Za-z0-9._:-]+$",
                description="A fresh key per refund, reused on every retry of that same refund",
            ),
        ],
        credits: Annotated[
            str | None,
            Field(
                description="Credits to give back, a positive decimal string; omit to refund all that is left"
            ),
        ] = None,
        reason: Annotated[str | None, Field(min_length=1, max_length=500)] = None,
        confirm: Annotated[
            bool,
            Field(
                description="false (default): only show what would be refunded. true: refund, which gives "
                "credits back to the customer's pool and cannot be undone"
            ),
        ] = False,
    ) -> dict:
        """Give back credits charged for one COMMITTED AI operation, in full or in part. It moves a customer's
        credits: call it first without confirm to see the charge, what was refunded and what is left, then with
        confirm=true. The refund lands in the usage window open now, through a negative report that is never
        refused by the pool's limit. idempotency_key makes a retry safe: the same key and body answer the original
        refund and move nothing; the same key with another body is refused. The reason is visible to anyone who
        can read the organization's instances. Answers the refund and what is left to refund."""
        islug, eslug = shapes.slug(instance, "instance"), shapes.slug(entitlement, "entitlement")
        if credits is not None:
            decimal_string(credits, "credits", 12, 6)
            if Decimal(credits) <= 0:
                raise ToolError("credits must be positive; omit it to refund all that is left.")
        if reason is not None and not reason.strip():
            raise ToolError("reason cannot be blank.")
        body = shapes.drop_none({"credits": credits, "reason": reason})

        def standing(row: dict) -> dict:
            got = row.get("credits") or {}
            charged = Decimal(got.get("charged") or "0")
            refunded = Decimal(got.get("refunded") or "0")
            return {
                "status": row.get("status"),
                "charged": ai_pricing.fmt(charged),
                "refunded": ai_pricing.fmt(refunded),
                "refundable": ai_pricing.fmt(max(charged - refunded, Decimal(0))),
            }

        if not confirm:
            row = await inference(islug, eslug, transaction_id)
            now = standing(row)
            target = {
                "instance": islug,
                "entitlement": eslug,
                "transaction_id": transaction_id,
                **now,
            }
            left = Decimal(now["refundable"])
            refusal = None
            if row.get("status") != "COMMITTED":
                refusal = f"RefundAiInference.NotCommitted: the operation is {row.get('status')}; only a commit is refunded"
            elif left <= 0:
                refusal = (
                    "RefundAiInference.NothingToRefund: it charged nothing, or is refunded in full"
                )
            elif credits is not None and Decimal(credits) > left:
                refusal = (
                    f"RefundAiInference.ExceedsRefundable: at most {now['refundable']} is left"
                )
            if refusal:
                return shapes.report(
                    "rejected",
                    "refund",
                    target,
                    reason=refusal,
                    notes=["Nothing was sent; Kaiten would refuse this refund."],
                )
            amount = Decimal(credits) if credits is not None else left
            return shapes.report(
                "preview",
                "refund",
                {
                    **target,
                    "would_refund": ai_pricing.fmt(amount),
                    "left_after": ai_pricing.fmt(left - amount),
                    "reason": reason,
                },
                notes=[
                    "Nothing was refunded. Call again with confirm=true and the same idempotency_key to refund."
                ],
            )
        try:
            refund, headers = await sdk.call_unwrapped_with_headers(
                "POST",
                f"{pool_path(islug, eslug)}/inferences/{transaction_id}/refund",
                body,
                idempotent=True,
                headers={"Idempotency-Key": idempotency_key},
            )
        except NotFoundError as e:
            if (e.code or "").startswith("RefundAiInference."):
                raise
            raise ToolError(
                f"This Kaiten does not serve AI refunds ({e.detail or 'not found'})."
            ) from e
        replayed = (headers.get("idempotent-replayed") or "").lower() == "true"
        after = standing(await inference(islug, eslug, transaction_id))
        return shapes.report(
            "recorded",
            "refund",
            refund,
            inference={
                "instance": islug,
                "entitlement": eslug,
                "transaction_id": transaction_id,
                **after,
            },
            replayed=True if replayed else None,
            notes=["A replay of the refund made earlier under this key: nothing moved again."]
            if replayed
            else None,
        )

    @mcp.tool(annotations=DESTRUCTIVE)
    async def kaiten_archive_ai_rule(
        license: str,
        rule_id: str,
        confirm: Annotated[
            bool,
            Field(
                description="false (default): only show the rule and what would price its calls instead. "
                "true: archive it"
            ),
        ] = False,
    ) -> dict:
        """Archive an AI conversion rule of one exact license VERSION: it stops pricing at once, for every
        instance on that version, and an archived rule cannot be edited or restored (create a new one). Call it
        first without confirm to see the rule and which rule would price its calls instead (a less specific one,
        or none: the calls become unpriced), then with confirm=true. Past charges keep their own snapshot."""
        lslug = shapes.slug(license, "license")
        try:
            rule_id = str(UUID(rule_id))
        except ValueError as e:
            raise ToolError("rule_id must be a UUID returned by kaiten_get_ai_rules.") from e
        path = f"licenses/{lslug}/conversion-rules/{rule_id}"
        rule = await sdk.require(sdk.call_unwrapped("GET", path), f"conversion rule {rule_id!r}")
        if rule.get("status") != "ACTIVE":
            return shapes.report("unchanged", "rule", rule, notes=["Already archived."])
        if not confirm:
            others = [
                r
                for r in await sdk.call_unwrapped(
                    "GET",
                    f"licenses/{lslug}/conversion-rules",
                    params={"status": "ACTIVE", "entitlementSlug": rule.get("entitlementSlug")},
                )
                or []
                if r.get("id") != rule_id
            ]
            # A call this rule's selector describes; "any" keeps only rules that cover every case.
            probe = {"slug": rule.get("modelSlug") or "\u0000any", "status": "ACTIVE"}
            if rule.get("modelSlug") is None:
                others = [r for r in others if r.get("modelSlug") is None]
            if rule.get("requestType") is None:
                others = [r for r in others if r.get("requestType") is None]
            fallback = ai_pricing.resolve(others, probe, rule.get("requestType") or "COMPLETION")
            fallback.pop("shadowed_rules", None)
            return shapes.report(
                "preview",
                "rule",
                {**rule, "level": ai_pricing.level(rule)},
                instead=fallback
                if fallback.get("priced")
                else {"priced": False, "denial_reason": "NO_RULE"},
                notes=[
                    "Nothing was archived. Call again with confirm=true to archive.",
                    "'instead' is computed here from the active rules read now, for calls this rule's "
                    "selector describes; a wildcard rule may also be what prices other models today.",
                ],
            )
        await sdk.call_unwrapped("DELETE", path, idempotent=True)
        after = await sdk.call_unwrapped("GET", path)
        return shapes.report(
            "updated",
            "rule",
            after,
            changes=shapes.changes({"status": rule.get("status")}, {"status": after.get("status")}),
            notes=[f"Archived on {lslug}: instances on it stop being priced by this rule now."],
        )
