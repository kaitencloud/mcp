# Copyright 2026 KAITEN INC
# SPDX-License-Identifier: Apache-2.0

"""The Kaiten MCP server: intent-level tools over Kaiten's API."""

from __future__ import annotations

import os
from importlib.metadata import PackageNotFoundError, version

import httpx
from fastmcp import FastMCP
from starlette.requests import Request
from starlette.responses import JSONResponse

from .config import settings
from .sdk import KaitenErrors
from .tools import register_all

try:
    VERSION = version("kaitencloud-mcp")
except PackageNotFoundError:  # running from a source tree
    VERSION = "0.0.0"

INSTRUCTIONS = """\
Kaiten is the control plane of a B2B SaaS vendor: what each customer bought, what they may use, how much
they used, and where it runs. Everything belongs to one organization (the vendor), and is named by slug.

The model, top down:
- Customer: a client of the vendor. It runs one or more Instances (a deployment of the product for it).
- Instance: sits on one exact License version, optionally in a Deployment Zone, with a license term
  (start/end), an operational status and a commercial lifecycle stage.
- Plan = license family (e.g. 'premium'). Its versions are Licenses (e.g. 'premium-v3'), each DRAFT,
  PUBLISHED or ARCHIVED. A plan serves its default version, else its highest published one.
- A License grants Entitlements: BOOLEAN features, NUMBER quotas (and AI credits) metered by usage
  reports, and CONFIG values. A NUMBER grant is unlimited (-1), a hard limit, or soft with an overage %.
- Feature flags are evaluated through OpenFeature, with CEL targeting rules reading the customer's
  license and entitlements, and the instance, customer and zone (under __kaiten).
- Deployment zones run immutable Releases, each a set of versioned Components.

How to work:
- Start with kaiten_overview, find things with kaiten_search, and read with the kaiten_get_* tools.
- Writes merge: only what you pass changes. Every write answers with status created / updated /
  unchanged / rejected / deleted and exactly what changed; 'unchanged' means nothing was written.
- Look before you change something shared: kaiten_get_catalog(plan=...) shows the instances on each
  license version, and kaiten_delete without confirm previews what a deletion touches.
- Prefer a new license version (kaiten_save_license new_version_of=...) to editing a published version
  in place: editing changes every instance on it immediately.
- Usage reports are not idempotent; never repeat one that answered.
"""


def build(*, writes: bool | None = None) -> FastMCP:
    mcp = FastMCP(
        "Kaiten",
        instructions=INSTRUCTIONS,
        version=VERSION,
        website_url="https://kaiten.sh",
        middleware=[KaitenErrors()],
    )
    register_all(mcp, writes=not settings.read_only if writes is None else writes)

    @mcp.custom_route("/health", methods=["GET"], include_in_schema=False)
    async def health(_: Request) -> JSONResponse:
        try:
            async with httpx.AsyncClient(timeout=5.0) as c:
                r = await c.get(f"{settings.api_base_url}/healthz")
            # A Community Edition gateway serves /healthz to anyone; Kaiten Cloud's asks for a
            # token and answers 401. Either way, Kaiten answered at that address.
            api = {"reachable": r.status_code in (200, 401), "status": r.status_code}
        except httpx.HTTPError as e:
            api = {"reachable": False, "error": str(e)}
        return JSONResponse(
            {
                "status": "ok" if api["reachable"] else "degraded",
                "version": VERSION,
                "read_only": settings.read_only,
                "api": {"url": settings.api_base_url, **api},
            }
        )

    return mcp


def http_app(mcp: FastMCP):
    """Stateless Streamable HTTP, with no process-token fallback for API calls.

    Discovery is public; the SDK requires a caller Bearer token for upstream calls,
    and Kaiten validates it. Host/Origin protection is not caller authentication.
    """
    allowed_hosts = [h for h in os.getenv("MCP_ALLOWED_HOSTS", "").split(",") if h.strip()]
    allowed_origins = [o for o in os.getenv("MCP_ALLOWED_ORIGINS", "").split(",") if o.strip()]
    return mcp.http_app(
        path="/mcp",
        stateless_http=True,
        host_origin_protection="auto",
        allowed_hosts=allowed_hosts or None,
        allowed_origins=allowed_origins or None,
    )
