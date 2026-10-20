# Copyright 2026 KAITEN INC
# SPDX-License-Identifier: Apache-2.0

"""Feature flags: definition, targeting rules, and evaluation.

Rules are CEL expressions over the evaluation context. Kaiten enriches the
context server-side under __kaiten: the license and entitlements of the
customer named by targetingKey, and the instance, customer and zone named by
kaiten.instanceSlug. A rule that cannot run does not fail the evaluation, it
just never matches, which is why every write here lints the rules first.

Rules are shown in the same shape the save tool accepts, so a read can be
edited and written back; the wire shapes are the SDK's (kaitencloud.targeting).
"""

from __future__ import annotations

import asyncio
from typing import Annotated, Any

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from kaitencloud import targeting
from kaitencloud.types import FeatureFlagType
from pydantic import Field

from .. import sdk, shapes, views
from . import READ, WRITE

RULES_DESCRIPTION = (
    "The complete, ordered list of targeting rules (first match wins); omit to keep the current rules, [] to remove "
    "them all. Each rule is {name, rule: CEL, variant} to serve one variant, {name, rule, rollout: {variant: weight}} "
    "to split matching traffic, or {name, rule, gradual: {from: {variant, percentage, date}, to: {...}}} for a "
    "date-driven rollout. Facts: __kaiten.license.slug, __kaiten.entitlements['<slug>'] (a bool, a config object, or "
    "{used, limit, remaining, percentage (0-1), unlimited}), __kaiten.instance.{slug,status,metadata}, "
    "__kaiten.customer.{slug,domain}, __kaiten.deploymentZone.{slug,type,metadata}, targetingKey, plus any attribute "
    "your app sends. kaiten_test_rule lists them all"
)


def rule_out(t: dict) -> dict:
    out = {"name": t.get("name"), "rule": t.get("rule")}
    kind = t.get("type")
    if kind == "basic":
        out["variant"] = t.get("variant")
    elif kind == "rollout_percentage":
        out["rollout"] = t.get("distribution")
    elif kind == "rollout_date":
        out["gradual"] = {"from": t.get("start"), "to": t.get("end")}
    else:
        return t
    return out


def rule_in(r: dict) -> dict:
    if "type" in r:
        return r
    if not r.get("name") or not r.get("rule"):
        raise ToolError(f"A targeting rule needs a name and a CEL rule: {r!r}")
    if "variant" in r:
        return targeting.rule(r["name"], r["rule"], r["variant"])
    if "rollout" in r:
        return targeting.split_rule(r["name"], r["rule"], r["rollout"])
    if "gradual" in r:
        g = r["gradual"]
        return targeting.schedule_rule(r["name"], r["rule"], start=g["from"], end=g["to"])
    raise ToolError(f"Rule {r['name']!r} needs one of: variant, rollout, gradual.")


def default_out(d: dict | None) -> Any:
    if not d:
        return None
    if d.get("type") == "basic":
        return d.get("value")
    if d.get("type") == "rollout_percentage":
        return {"rollout": d.get("distribution")}
    if d.get("type") == "rollout_date":
        return {"gradual": {"from": d.get("start"), "to": d.get("end")}}
    return d


def default_in(d: Any) -> dict:
    if isinstance(d, str):
        return targeting.fixed(d)
    if isinstance(d, dict) and "type" in d:
        return d
    if isinstance(d, dict) and "rollout" in d:
        return targeting.split(d["rollout"])
    if isinstance(d, dict) and "gradual" in d:
        return targeting.schedule(start=d["gradual"]["from"], end=d["gradual"]["to"])
    raise ToolError(
        "default is a variant name, {rollout: {variant: weight}} or {gradual: {from, to}}."
    )


def wire(f: Any) -> dict:
    return f.to_dict() if hasattr(f, "to_dict") else f


def flag_summary(f: Any) -> dict:
    w = wire(f)
    return shapes.drop_none(
        {
            "slug": w.get("slug"),
            "name": w.get("name"),
            "type": w.get("type"),
            "enabled": w.get("enabled"),
            "default": default_out(w.get("default_variant")),
            "variants": [v["name"] for v in w.get("variants") or []],
            "rules": len(w.get("targetings") or []),
        }
    )


def flag_detail(f: Any) -> dict:
    w = wire(f)
    return shapes.drop_none(
        {
            "slug": w.get("slug"),
            "name": w.get("name"),
            "description": w.get("description") or None,
            "type": w.get("type"),
            "enabled": w.get("enabled"),
            "variants": {v["name"]: v.get("value") for v in w.get("variants") or []},
            "default": default_out(w.get("default_variant")),
            "rules": [rule_out(t) for t in w.get("targetings") or []],
            "metadata": w.get("metadata") or None,
        }
    )


async def lint(rules: list[dict]) -> list[dict]:
    """Every rule the write path would refuse, with the reason."""
    rules = [r for r in rules if r.get("rule")]
    verdicts = await asyncio.gather(
        *(
            sdk.call_unwrapped(
                "POST", "feature-flags/targeting/lint", {"rule": r["rule"]}, idempotent=True
            )
            for r in rules
        )
    )
    return [
        {"rule": r.get("name"), "issues": [i["message"] for i in v.get("issues") or []]}
        for r, v in zip(rules, verdicts, strict=True)
        if not v.get("valid")
    ]


async def evaluation_context(
    customer: str | None, instance: str | None, context: dict | None
) -> dict:
    ctx = dict(context or {})
    if instance:
        inst = await views.instance(instance)
        ctx.setdefault("kaiten", {})["instanceSlug"] = inst.slug
        customer = customer or inst.customer_slug
    if customer:
        ctx["targetingKey"] = shapes.slug(customer, "customer")
    return ctx


def register(mcp: FastMCP, *, writes: bool) -> None:
    @mcp.tool(annotations=READ)
    async def kaiten_get_flags(
        flag: Annotated[
            str | None, Field(description="A flag slug, for its full definition")
        ] = None,
    ) -> dict:
        """Feature flags. Without a flag: one line per flag (type, enabled, default, rule count). With a flag: its
        variants, default, ordered targeting rules (linted) and metadata, in the shape kaiten_save_flag accepts.
        To see what a customer actually gets, and which rules fail at evaluation, use kaiten_evaluate_flags."""
        k = sdk.kaiten()
        if flag:
            f = await sdk.require(
                k.feature_flags.get(shapes.slug(flag, "flag")), f"feature flag {flag!r}"
            )
            out = flag_detail(f)
            broken = await lint(wire(f).get("targetings") or [])
            if broken:
                out["broken_rules"] = broken
            return out
        flags = await k.feature_flags.list()
        return {"total": len(flags), "flags": [flag_summary(f) for f in flags]}

    @mcp.tool(annotations=READ)
    async def kaiten_evaluate_flags(
        customer: Annotated[
            str | None, Field(description="Evaluate as this customer (the targeting key)")
        ] = None,
        instance: Annotated[
            str | None, Field(description="Evaluate as this instance; implies its customer")
        ] = None,
        context: Annotated[
            dict[str, Any] | None,
            Field(
                description="Extra attributes your app would send, e.g. {'user': {'role': 'admin'}}"
            ),
        ] = None,
        flags: Annotated[list[str] | None, Field(description="Only these flag slugs")] = None,
    ) -> dict:
        """What a customer or instance actually gets: evaluates flags exactly as your application would through
        OpenFeature, with Kaiten's server-side facts, and returns each flag's value, variant and reason
        (TARGETING_MATCH, SPLIT, DEFAULT, DISABLED, ERROR), plus every rule that failed to run and why."""
        ctx = await evaluation_context(customer, instance, context)
        result = await sdk.call_unwrapped(
            "POST", "ofrep/v1/evaluate/flags", {"context": ctx}, idempotent=True
        )
        out = {}
        for f in result.get("flags") or []:
            if flags and f["key"] not in flags:
                continue
            row = shapes.drop_none(
                {
                    "value": f.get("value"),
                    "variant": f.get("variant"),
                    "reason": f.get("reason"),
                    "error": f.get("errorCode"),
                }
            )
            broken = (f.get("metadata") or {}).get("kaiten.brokenTargetingRules")
            if broken:
                row["broken_rules"] = broken
            out[f["key"]] = row
        return {"context": ctx, "flags": out}

    @mcp.tool(annotations=READ)
    async def kaiten_test_rule(
        rule: Annotated[
            str | None,
            Field(description="A CEL targeting rule; omit to list the facts a rule can read"),
        ] = None,
        customer: Annotated[str | None, Field(description="Test against this customer")] = None,
        instance: Annotated[
            str | None, Field(description="Test against this instance (and its customer)")
        ] = None,
        context: Annotated[
            dict[str, Any] | None, Field(description="Extra attributes your app would send")
        ] = None,
    ) -> dict:
        """Author a targeting rule safely. With a rule: lints it, runs it against a customer or instance exactly as an
        evaluation would, and returns whether it matched and the facts it read. Without one: the facts Kaiten
        provides under __kaiten, with their types and known keys. Writes nothing."""
        if not rule:
            return await sdk.call_unwrapped("GET", "feature-flags/targeting/context")
        ctx = await evaluation_context(customer, instance, context)
        body = shapes.drop_none(
            {"rule": rule, "targetingKey": ctx.pop("targetingKey", None), "context": ctx or None}
        )
        r = await sdk.call_unwrapped("POST", "feature-flags/targeting/test", body, idempotent=True)
        return shapes.drop_none(
            {
                "valid": r.get("valid"),
                "matched": r.get("matched")
                if r.get("valid") and not r.get("evaluationError")
                else None,
                "issues": [i["message"] for i in r.get("issues") or []] or None,
                "evaluation_error": r.get("evaluationError"),
                "facts": r.get("facts"),
            }
        )

    if not writes:
        return

    @mcp.tool(annotations=WRITE)
    async def kaiten_save_flag(
        flag: Annotated[
            str,
            Field(
                description="Flag slug (the key your app evaluates). Created when it does not exist yet"
            ),
        ],
        name: Annotated[str | None, Field(description="Display name (required to create)")] = None,
        description: str | None = None,
        type: Annotated[
            FeatureFlagType | None,
            Field(description="Value type, set at creation (defaults to boolean)"),
        ] = None,
        enabled: Annotated[
            bool | None,
            Field(
                description="The kill switch: a disabled flag serves its default to everyone and runs no rule"
            ),
        ] = None,
        variants: Annotated[
            dict[str, Any] | None,
            Field(
                description="The complete set of variants, name -> value. A boolean flag defaults to {on: true, off: false}"
            ),
        ] = None,
        default: Annotated[
            Any,
            Field(
                description="Served when no rule matches: a variant name, or {rollout: {variant: weight}}"
            ),
        ] = None,
        rules: Annotated[list[dict[str, Any]] | None, Field(description=RULES_DESCRIPTION)] = None,
        metadata: Annotated[
            dict[str, Any] | None, Field(description="Keys to set; null removes a key")
        ] = None,
    ) -> dict:
        """Create a feature flag or change one: toggle it, change its variants or default, or replace its targeting
        rules. Every rule is linted before anything is written; a broken rule is refused with the reason, rather than
        saved to silently never match. Only what you pass changes."""
        k = sdk.kaiten()
        fslug = shapes.slug(flag, "flag")
        cur = await sdk.get_or_none(k.feature_flags.get(fslug))
        if cur is None and not name:
            raise ToolError(f"Flag {fslug!r} does not exist yet: pass a name to create it.")
        base = (
            wire(cur)
            if cur
            else {
                "name": name,
                "description": "",
                "type": type or "boolean",
                "enabled": True,
                "variants": [],
                "targetings": [],
                "metadata": {},
                "event_name": f"{fslug}.evaluated",
            }
        )
        if cur and type and type != base["type"]:
            raise ToolError(
                f"Flag {fslug} is a {base['type']} flag; its type cannot change. Create a new flag instead."
            )
        body: dict[str, Any] = {
            "name": base["name"] if name is None else name,
            "description": (base.get("description") or "") if description is None else description,
            "type": base["type"],
            "enabled": base["enabled"] if enabled is None else enabled,
            "variants": base.get("variants") or [],
            "targetings": base.get("targetings") or [],
            "metadata": shapes.merge_map(base.get("metadata"), metadata),
            "event_name": base.get("event_name") or f"{fslug}.evaluated",
            "default_variant": base.get("default_variant"),
        }
        if variants is None and cur is None:
            if body["type"] != "boolean":
                raise ToolError("A non-boolean flag needs its variants.")
            variants = {"on": True, "off": False}
        if variants is not None:
            known = {v["name"]: v.get("description", "") for v in body["variants"]}
            body["variants"] = [
                targeting.variant(n, v, description=known.get(n, "")) for n, v in variants.items()
            ]
        if default is not None:
            body["default_variant"] = default_in(default)
        elif cur is None:
            names = [v["name"] for v in body["variants"]]
            body["default_variant"] = targeting.fixed("off" if "off" in names else names[0])
        if rules is not None:
            body["targetings"] = [rule_in(r) for r in rules]

        names = {v["name"] for v in body["variants"]}
        dv = body["default_variant"] or {}
        referenced = (
            [dv.get("value")] if dv.get("type") == "basic" else list(dv.get("distribution") or {})
        )
        for t in body["targetings"]:
            referenced += (
                [t["variant"]] if t.get("type") == "basic" else list(t.get("distribution") or {})
            )
        unknown = sorted({r for r in referenced if r and r not in names})
        if unknown:
            raise ToolError(
                f"Unknown variant(s) {unknown}; the flag's variants are {sorted(names)}."
            )
        broken = await lint(body["targetings"])
        if broken:
            return shapes.report(
                "rejected",
                "flag",
                flag_detail(cur) if cur else None,
                broken_rules=broken,
                notes=[
                    "Nothing was written. Fix the rules (kaiten_test_rule helps) and save again."
                ],
            )

        if cur is None:
            created = await k.feature_flags.create(slug=fslug, **body)
            return shapes.report("created", "flag", flag_detail(created))
        diff = shapes.changes(flag_detail(cur), flag_detail({**base, **body}))
        if not diff:
            return shapes.report("unchanged", "flag", flag_detail(cur))
        # Not k.feature_flags.update: it omits the slug, and Kaiten writes the
        # body's slug verbatim, so an update through it blanks the flag's slug
        # and orphans the flag. Send the same body with the slug kept.
        await sdk.call_unwrapped("PUT", f"feature-flags/{fslug}", {**body, "slug": fslug})
        return shapes.report(
            "updated", "flag", flag_detail(await k.feature_flags.get(fslug)), changes=diff
        )
