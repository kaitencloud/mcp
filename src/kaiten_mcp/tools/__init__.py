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


def register_all(mcp: FastMCP, *, writes: bool, ai_enabled: bool = False) -> None:
    from . import (
        access,
        accounts,
        billing,
        catalog,
        delete,
        flags,
        overview,
        releases,
        settings,
        usage,
        webhooks,
    )

    ai_enabled = ai_enabled is True
    # The modules that read or write an entitlement take the rollout decision, so
    # that an AI credit pool stays invisible while it is off; the others cannot
    # reach one.
    aware = (overview, accounts, usage, catalog, flags, delete)
    for module in (
        overview,
        accounts,
        usage,
        catalog,
        billing,
        flags,
        releases,
        access,
        settings,
        webhooks,
        delete,
    ):
        if module in aware:
            module.register(mcp, writes=writes, ai_enabled=ai_enabled)
        else:
            module.register(mcp, writes=writes)
    if ai_enabled:
        from . import ai

        ai.register(mcp, writes=writes)
