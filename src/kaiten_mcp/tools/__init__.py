# Copyright 2026 KAITEN INC
# SPDX-License-Identifier: Apache-2.0

"""The tool catalogue, one module per area of Kaiten.

Every module registers its read tools always, and its write tools only when
the server is not read-only. Annotations tell the client which is which.
"""

from __future__ import annotations

from fastmcp import FastMCP

# Kaiten is the only world these tools touch, so none is open-world.
READ = {"readOnlyHint": True, "openWorldHint": False}
WRITE = {
    "readOnlyHint": False,
    "destructiveHint": False,
    "idempotentHint": True,
    "openWorldHint": False,
}
# A write whose repetition is not a no-op: a usage report counts every time.
APPEND = {
    "readOnlyHint": False,
    "destructiveHint": False,
    "idempotentHint": False,
    "openWorldHint": False,
}
DESTRUCTIVE = {
    "readOnlyHint": False,
    "destructiveHint": True,
    "idempotentHint": True,
    "openWorldHint": False,
}


def register_all(
    mcp: FastMCP, *, writes: bool, ai_enabled: bool = False, hide_ai_entitlements: bool = False
) -> None:
    """Register the catalogue.

    ai_enabled registers the AI credit tools (models, rules, history, charges, refunds). It does
    not change the generic tools: since 0.2.0 they list, create and meter NUMBER_AI_CREDIT
    entitlements like any other, and turning the AI tools off must not take that away.
    hide_ai_entitlements is the stricter projection, off unless a caller asks for it: the generic
    tools then omit and refuse AI credit entitlements.
    """
    from . import access, accounts, catalog, delete, flags, overview, releases, settings, usage

    ai_enabled = ai_enabled is True
    # The generic modules' ai_enabled says whether AI entitlements are visible to them.
    visible = hide_ai_entitlements is not True
    for module in (overview, accounts, usage, catalog, flags, releases, access, settings, delete):
        if module in (overview, accounts, usage, catalog, flags, delete):
            module.register(mcp, writes=writes, ai_enabled=visible)
        else:
            module.register(mcp, writes=writes)
    if ai_enabled:
        from . import ai

        ai.register(mcp, writes=writes)
