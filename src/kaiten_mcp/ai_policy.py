# Copyright 2026 KAITEN INC
# SPDX-License-Identifier: Apache-2.0

"""Local discovery/attempt policy, never a substitute for server authorization.

Only contract type discriminators classify operational rows. Arbitrary config,
metadata and immutable financial/audit payloads are not inspected or rewritten.
"""

from typing import Any

from fastmcp.exceptions import ToolError


def is_ai(resource: Any, definition: Any = None) -> bool:
    return (
        getattr(resource, "type", None) == "NUMBER_AI_CREDIT"
        or getattr(resource, "entitlement_type", None) == "NUMBER_AI_CREDIT"
        or getattr(definition, "type", None) == "NUMBER_AI_CREDIT"
    )


def require_available(resource: Any, *, enabled: bool, definition: Any = None) -> None:
    if enabled is not True and is_ai(resource, definition):
        raise ToolError("This entitlement is not available in this MCP deployment.")


def visible(resource: Any, *, enabled: bool, definition: Any = None) -> bool:
    return enabled is True or not is_ai(resource, definition)


def targeting_context(payload: dict, *, enabled: bool) -> dict:
    """Project only Core's reserved, typed entitlement-schema node.

    Core's TargetingContextNode annotates known entries with entitlementType;
    an old/unknown original type is not evidence that an entry is ordinary.
    No catalogue read (and no additional scope) is required for authoring.
    """
    if enabled is True:
        return payload
    roots = payload.get("roots")
    if not isinstance(roots, list):
        raise ToolError("The server did not return a structured targeting context.")
    projected = []
    for root in roots:
        if not isinstance(root, dict):
            raise ToolError("The server did not return a structured targeting context.")
        if root.get("name") != "__kaiten":
            projected.append(root)
            continue
        fields = []
        for node in root.get("fields") or []:
            if not isinstance(node, dict):
                raise ToolError("The server did not return a structured targeting context.")
            if node.get("name") != "entitlements":
                fields.append(node)
                continue
            entries = [
                entry
                for entry in node.get("entries") or []
                if isinstance(entry, dict)
                and entry.get("entitlementType") in ("NUMBER", "BOOLEAN", "CONFIG")
            ]
            names = {entry.get("name") for entry in entries}
            fields.append(
                {
                    **node,
                    "entries": entries,
                    "knownKeys": [key for key in node.get("knownKeys") or [] if key in names],
                }
            )
        projected.append({**root, "fields": fields})
    return {**payload, "roots": projected}


def targeting_facts(facts: Any, types: Any, *, enabled: bool) -> Any:
    """Facts are the __kaiten subroots, not arbitrary user context/metadata.

    The rehearsal's separate entitlementTypes map preserves original wire types
    even when the numeric fact shape is shared with AI. Unclassified entries from
    older servers are omitted rather than guessing from values or slug names.
    """
    if enabled is True or facts is None:
        return facts
    if not isinstance(facts, dict):
        raise ToolError("The server did not return structured targeting facts.")
    entitlements = facts.get("entitlements")
    if entitlements is None:
        return facts
    if not isinstance(entitlements, dict):
        raise ToolError("The server did not return structured entitlement facts.")
    declared = types if isinstance(types, dict) else {}
    return {
        **facts,
        "entitlements": {
            slug: value
            for slug, value in entitlements.items()
            if declared.get(slug) in ("NUMBER", "BOOLEAN", "CONFIG")
        },
    }


async def require_license_available(license_slug: str, *, enabled: bool) -> None:
    """Preflight an operation affecting all grants using canonical, unfiltered rows."""
    if enabled is True:
        return
    from . import sdk

    k = sdk.kaiten()
    definitions = {e.slug: e for e in await k.entitlements.list()}
    for grant in await k.licenses.list_entitlements(license_slug):
        require_available(
            grant, enabled=enabled, definition=definitions.get(grant.entitlement_slug)
        )
