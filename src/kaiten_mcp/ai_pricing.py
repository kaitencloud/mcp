# Copyright 2026 KAITEN INC
# SPDX-License-Identifier: Apache-2.0

"""AI credit pricing, read back: which rule a call would meet, and how a charge was made.

Pure functions over the API's JSON, no requests. Kaiten prices every call itself; what is
here only re-reads its rules and its records so an agent can explain them. Two things:

- ``resolve`` walks a licence version's rules with the server's four-level ladder: the
  model's own rule for the request type, then the model's own rule for every type, then
  any model for the type, then any model for every type. Unknown and archived models are
  never priced, not even by a wildcard. It is a client-side reading of the rules as they
  are now; the server decides at report time, on the rules it holds then.
- ``explain`` rebuilds an inference's arithmetic from the row alone: the pricing snapshot
  it was charged with and the usage it stored. It never reads today's rules, which may
  have changed since.

Every reader tolerates members and warning codes it does not know: the contract adds
them, and an older or newer server must still be explained.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any

# Best first: the server's ladder.
LEVELS = ("EXACT_TYPED", "EXACT_ALL", "ANY_TYPED", "ANY_ALL")
LEVEL_MEANING = {
    "EXACT_TYPED": "the model's own rule for this request type",
    "EXACT_ALL": "the model's own rule, for every request type",
    "ANY_TYPED": "a wildcard rule for this request type, any model",
    "ANY_ALL": "a wildcard rule, any model and any request type",
}
REQUEST_TYPES = ("COMPLETION", "EMBEDDING", "IMAGE_GENERATION", "AUDIO", "VIDEO")

DENIAL_MEANING = {
    "UNKNOWN_MODEL": "the model as sent matches no catalogue model (slug or external id): never priced",
    "MODEL_ARCHIVED": "the model is ARCHIVED: never priced, not even by a wildcard",
    "NO_RULE": "no active rule of this licence version and pool matches the model and request type",
}

WARNING_MEANING = {
    "MODEL_DEPRECATED": "the model is DEPRECATED; its rules still price it",
    "ESTIMATE_INCOMPLETE": "the reserve carried no quantity a rate applies to: it held the rule's minimum",
    "RESERVATION_EXPIRED": "the commit arrived after its reservation had expired or closed",
    "MODEL_NOT_AUTHORIZED": "the model is known but no rule authorizes it (see denialReason)",
    "UNKNOWN_MODEL": "the model as sent is not in the catalogue",
    "CONVERSION_INPUT_MISSING": "the commit carried no quantity a rate applies to: the rule's minimum was computed",
    "OVERDRAWN": "the pool was already past its maximum when this commit was counted",
    "NO_RESERVATION": "a release for a transaction that never reserved: nothing moved, the id is closed",
    "COUNTER_PRECISION_DEGRADED": "the usage counter could not hold the exact charge; credits.charged is exact",
    "PRICING_SNAPSHOT_UNREADABLE": "the stored pricing snapshot could not be decoded: the row is listed without it",
}

# Rate field -> the usage quantity it multiplies, and the rate it falls back to when absent.
TERMS = (
    ("perRequest", None, None),
    ("perInputToken", "inputTokens", None),
    ("perCachedInputToken", "cacheReadInputTokens", "perInputToken"),
    ("perCacheWriteToken", "cacheWriteInputTokens", "perInputToken"),
    ("perOutputToken", "outputTokens", None),
    ("perUnit", "units", None),
    ("perSecond", "durationMs", None),
)
KNOWN_RATES = {term[0] for term in TERMS}
QUANTUM = Decimal("0.000001")
CLIENT_SIDE = (
    "Client-side resolution from the rules and models as read now, with the server's ladder. "
    "Kaiten decides at report time on the rules it holds then; this is not a quote."
)


def decimal(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def fmt(value: Decimal) -> str:
    """A decimal as plain digits, without exponent or trailing zeros."""
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def level(rule: dict) -> str:
    model, typed = rule.get("modelSlug") is not None, rule.get("requestType") is not None
    if model:
        return "EXACT_TYPED" if typed else "EXACT_ALL"
    return "ANY_TYPED" if typed else "ANY_ALL"


def find_model(models: list[dict], sent: str) -> dict | None:
    """The catalogue model a call names: its slug, or one of its exact external ids."""
    for model in models:
        if model.get("slug") == sent:
            return model
    for model in models:
        if sent in (model.get("externalIds") or []):
            return model
    return None


def resolve(rules: list[dict], model: dict | None, request_type: str) -> dict:
    """Which active rule would price a call, by the server's ladder.

    model is the catalogue model the call resolves to, or None when it resolves to none.
    """
    if model is None:
        return {"priced": False, "denial_reason": "UNKNOWN_MODEL"}
    if model.get("status") == "ARCHIVED":
        return {"priced": False, "denial_reason": "MODEL_ARCHIVED"}
    slug = model.get("slug")
    matches: dict[str, list[dict]] = {}
    for rule in rules:
        if rule.get("status", "ACTIVE") != "ACTIVE":
            continue
        if rule.get("modelSlug") not in (None, slug):
            continue
        if rule.get("requestType") not in (None, request_type):
            continue
        matches.setdefault(level(rule), []).append(rule)
    for lvl in LEVELS:
        if lvl in matches:
            out: dict[str, Any] = {
                "priced": True,
                "rule_id": matches[lvl][0].get("id"),
                "specificity": lvl,
                "meaning": LEVEL_MEANING[lvl],
            }
            if len(matches[lvl]) > 1:
                out["ambiguous"] = [r.get("id") for r in matches[lvl]]
            shadowed = [
                r.get("id") for other in LEVELS if other != lvl for r in matches.get(other, [])
            ]
            if shadowed:
                out["shadowed_rules"] = shadowed
            if model.get("status") == "DEPRECATED":
                out["warning"] = "MODEL_DEPRECATED"
            return out
    return {"priced": False, "denial_reason": "NO_RULE"}


def warnings_of(codes: Any) -> list[dict]:
    """Warning codes with what they mean; a code this server version does not know is kept."""
    out = []
    for code in codes if isinstance(codes, list) else []:
        meaning = WARNING_MEANING.get(code)
        out.append(
            {"code": code, "meaning": meaning}
            if meaning
            else {"code": code, "meaning": "not known to this MCP version; shown as Kaiten sent it"}
        )
    return out


def _allowed(request_status: str | None, policy: str | None) -> tuple[bool, str]:
    status = request_status or "SUCCESS"
    if status == "SUCCESS":
        return True, "SUCCESS is always charged"
    if status == "NOT_SENT":
        return False, "NOT_SENT is never charged, whatever chargeOnError says"
    if status == "PROVIDER_ERROR":
        ok = policy in ("ALWAYS", "PROVIDER_ERROR_ONLY")
        return (
            ok,
            f"PROVIDER_ERROR with chargeOnError {policy}: {'charged' if ok else 'not charged'}",
        )
    if status == "CUSTOMER_ERROR":
        ok = policy == "ALWAYS"
        return (
            ok,
            f"CUSTOMER_ERROR with chargeOnError {policy}: {'charged' if ok else 'not charged'}",
        )
    return False, f"request status {status} is not known to this MCP version; not recomputed"


def formula(pricing: dict, usage: dict, request_status: str | None) -> dict:
    """The snapshot's arithmetic over the stored usage, as Kaiten does it: rate x quantity for
    every rate and quantity present (cache rates falling back to the input rate, perSecond on
    durationMs / 1000), the minimum when nothing applies, the error policy, then the clamp to
    [minimum, maximum], rounded half-up to six places."""
    rates = pricing.get("rates") if isinstance(pricing.get("rates"), dict) else {}
    usage = usage if isinstance(usage, dict) else {}
    lines: list[str] = []
    incomplete: list[str] = []
    for key in sorted(set(rates) - KNOWN_RATES):
        incomplete.append(f"rate {key} is not known to this MCP version: not recomputed")
    total = Decimal(0)
    present = False
    for rate_key, quantity_key, fallback in TERMS:
        raw_rate, used = rates.get(rate_key), rate_key
        if raw_rate is None and fallback is not None and rates.get(fallback) is not None:
            raw_rate, used = (
                rates.get(fallback),
                f"{fallback} (no {rate_key}: the input rate applies)",
            )
        rate = decimal(raw_rate)
        if rate is None:
            continue
        if quantity_key is None:
            quantity, label = Decimal(1), "1 request"
        else:
            quantity = decimal(usage.get(quantity_key))
            if quantity is None:
                continue
            label = f"{quantity_key} {fmt(quantity)}"
            if quantity_key == "durationMs":
                quantity = quantity.scaleb(-3)
                label = f"durationMs {fmt(quantity * 1000)} = {fmt(quantity)} s"
        product = rate * quantity
        total += product
        present = True
        lines.append(f"{label} x {used} {fmt(rate)} = {fmt(product)}")
    minimum = decimal(pricing.get("minimumCreditsPerRequest")) or Decimal(0)
    maximum = decimal(pricing.get("maximumCreditsPerRequest"))
    if not present:
        total = minimum
        lines.append(f"no quantity a rate applies to: computed = minimum {fmt(minimum)}")
    else:
        lines.append(f"sum = {fmt(total)}")
    computed = total.quantize(QUANTUM, rounding=ROUND_HALF_UP)
    allowed, policy = _allowed(request_status, pricing.get("chargeOnError"))
    if allowed:
        charged = max(total, minimum)
        clamp = f"max(computed, minimum {fmt(minimum)}) = {fmt(charged)}"
        if maximum is not None:
            charged = min(charged, maximum)
            clamp += f"; min(that, maximum {fmt(maximum)}) = {fmt(charged)}"
        else:
            clamp += "; no maximum"
    else:
        charged, clamp = Decimal(0), "not charged: 0"
    out: dict[str, Any] = {
        "lines": lines,
        "computed": fmt(computed),
        "policy": policy,
        "clamp": clamp,
        "charged": fmt(charged.quantize(QUANTUM, rounding=ROUND_HALF_UP)),
    }
    if incomplete:
        out["incomplete"] = incomplete
    return out


def _same(a: Any, b: Any) -> bool | None:
    x, y = decimal(a), decimal(b)
    return None if x is None or y is None else x == y


def explain(row: dict) -> dict:
    """An inference row, explained from itself."""
    credits = row.get("credits") if isinstance(row.get("credits"), dict) else {}
    status = row.get("status")
    warnings = row.get("warnings")
    codes = warnings if isinstance(warnings, list) else []
    refunds = row.get("refunds") if isinstance(row.get("refunds"), list) else []
    charged = decimal(credits.get("charged"))
    refunded = decimal(credits.get("refunded")) or Decimal(0)
    out: dict[str, Any] = {
        "transaction_id": row.get("transactionId"),
        "status": status,
        "request_status": row.get("requestStatus"),
        "request_type": row.get("requestType"),
        "model": {"as_sent": row.get("model"), "catalogue": row.get("modelSlug")},
        "license": {"slug": row.get("licenseSlug"), "id": row.get("licenseId")},
        "usage": row.get("usage"),
        "credits": {
            k: v
            for k, v in {
                "reserved": credits.get("reserved"),
                "computed": credits.get("computed"),
                "charged": credits.get("charged"),
                "refunded": credits.get("refunded"),
                "refundable": fmt(max(charged - refunded, Decimal(0)))
                if status == "COMMITTED" and charged is not None
                else None,
            }.items()
            if v is not None
        },
    }
    notes: list[str] = []
    for key, label in (("instanceSlug", "instance"), ("entitlementSlug", "entitlement")):
        if row.get(key) is not None:
            out[label] = row[key]
    if row.get("valueBefore") is not None or row.get("valueAfter") is not None:
        out["counter"] = {
            "before": row.get("valueBefore"),
            "after": row.get("valueAfter"),
            "window_start": row.get("windowStart"),
        }
        notes.append(
            "The counter is a float: before/after are the pool's usage in that window, "
            "not an exact balance. credits.charged is exact."
        )
    if row.get("reportSeq") is not None:
        out["report_seq"] = row["reportSeq"]
    if row.get("vendorCost") is not None:
        out["vendor_cost"] = row["vendorCost"]
    if row.get("authorized") is not None:
        out["authorized"] = row["authorized"]
    if row.get("denialReason"):
        out["denial"] = {
            "reason": row["denialReason"],
            "meaning": DENIAL_MEANING.get(
                row["denialReason"], "not known to this MCP version; shown as Kaiten sent it"
            ),
        }
    if "NO_RESERVATION" in codes:
        notes.append(
            "A release with no reservation (a tombstone): nothing was held or charged, and the "
            "transaction id is closed; a later reserve or commit under it answers 409 TransactionIdReused."
        )
    pricing = row.get("pricing")
    if isinstance(pricing, dict):
        out["priced_by"] = {
            "rule_id": pricing.get("ruleId"),
            "specificity": pricing.get("specificity"),
            "meaning": LEVEL_MEANING.get(pricing.get("specificity"), "unknown level"),
            "rates": pricing.get("rates"),
            "minimum": pricing.get("minimumCreditsPerRequest"),
            "maximum": pricing.get("maximumCreditsPerRequest"),
            "charge_on_error": pricing.get("chargeOnError"),
        }
        recomputed = formula(pricing, row.get("usage") or {}, row.get("requestStatus"))
        out["formula"] = recomputed
        if status in ("COMMITTED", "RELEASED"):
            checks = {
                "computed": _same(recomputed["computed"], credits.get("computed")),
                "charged": _same(recomputed["charged"], credits.get("charged")),
            }
        elif status == "RESERVED" or status == "EXPIRED":
            checks = {"reserved": _same(recomputed["charged"], credits.get("reserved"))}
            notes.append(
                "A reservation holds the estimate's charge; nothing is charged until the commit."
                if status == "RESERVED"
                else "The reservation expired: what it held stopped counting and nothing was charged."
            )
        else:
            checks = {}
        if any(v is False for v in checks.values()):
            notes.append(
                "The recomputed figure differs from Kaiten's: Kaiten's stored credits are authoritative."
            )
        out["matches_kaiten"] = checks
    elif "PRICING_SNAPSHOT_UNREADABLE" in codes:
        notes.append(
            "Kaiten could not decode the stored pricing snapshot: the charge cannot be rebuilt here. "
            "credits.charged is still what was charged."
        )
    elif row.get("authorized") is False or row.get("denialReason"):
        notes.append("No rule priced this operation, so there is no arithmetic to show.")
    elif "NO_RESERVATION" not in codes:
        notes.append(
            "This row carries no pricing snapshot: it was recorded before Kaiten kept snapshots, or "
            "by a server that does not send them. The charge is not rebuilt from today's rules, "
            "which may have changed since; credits.charged is what was charged."
        )
    out["warnings"] = warnings_of(codes)
    if refunds:
        out["refunds"] = refunds
    if notes:
        out["notes"] = notes
    return out
