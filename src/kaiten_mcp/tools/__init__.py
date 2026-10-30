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


def register_all(mcp: FastMCP, *, writes: bool) -> None:
    from . import (
        access,
        accounts,
        catalog,
        delete,
        flags,
        overview,
        releases,
        settings,
        usage,
        webhooks,
    )

    for module in (
        overview,
        accounts,
        usage,
        catalog,
        flags,
        releases,
        access,
        settings,
        webhooks,
        delete,
    ):
        module.register(mcp, writes=writes)
