"""
Kaiten MCP Server

Exposes the Kaiten API as MCP tools for AI assistants.
Forwards the client's Authorization header to the API.
"""

import httpx
import os
import uvicorn
from contextvars import ContextVar
from fastmcp import FastMCP
from fastmcp.server.openapi import RouteMap, MCPType
from starlette.applications import Starlette
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse
from starlette.routing import Route, Mount

# Configuration
API_PORT = os.getenv("KAITEN_API_PORT", "3080")
API_BASE_URL = os.getenv("KAITEN_API_BASE_URL", f"http://api:{API_PORT}/api")
API_OPENAPI_URL = os.getenv("KAITEN_API_OPENAPI_URL", f"{API_BASE_URL}/openapi.json")
MCP_PORT = int(os.getenv("MCP_PORT", "8000"))

# Context variable for per-request auth
auth_token: ContextVar[str] = ContextVar("auth_token", default="")


class AuthCapture(BaseHTTPMiddleware):
  """Captures Authorization header and stores in context."""

  async def dispatch(self, request, call_next):
    token = auth_token.set(request.headers.get("Authorization", ""))
    try:
      return await call_next(request)
    finally:
      auth_token.reset(token)


class ForwardAuth(httpx.Auth):
  """Forwards captured auth to API requests."""

  def auth_flow(self, request):
    if token := auth_token.get():
      request.headers["Authorization"] = token
    yield request


# HTTP client with dynamic auth
client = httpx.AsyncClient(
  base_url=API_BASE_URL,
  auth=ForwardAuth(),
  timeout=30.0,
)

def fetch_openapi_spec() -> dict:
  response = httpx.get(API_OPENAPI_URL, timeout=30.0, follow_redirects=True)
  response.raise_for_status()

  try:
    return response.json()
  except ValueError as exc:
    preview = response.text[:200].strip()
    raise RuntimeError(
      f"failed to decode OpenAPI spec from {API_OPENAPI_URL}: "
      f"status={response.status_code}, content_type={response.headers.get('content-type')}, body={preview!r}"
    ) from exc


# Fetch OpenAPI spec and create MCP server
openapi_spec = fetch_openapi_spec()

mcp = FastMCP.from_openapi(
  openapi_spec=openapi_spec,
  client=client,
  name="Kaiten API",
  route_maps=[
    RouteMap(pattern=r"^/api/tokens/validate", mcp_type=MCPType.EXCLUDE),
  ],
)


async def health_check(request):
  """Health check endpoint that verifies API connectivity."""
  try:
    # Try to reach the API health endpoint
    response = httpx.get(f"{API_BASE_URL}/healthz", timeout=5.0)
    api_status = "ok" if response.status_code == 200 else f"error ({response.status_code})"
    api_reachable = response.status_code == 200
  except Exception as e:
    api_status = f"unreachable ({str(e)})"
    api_reachable = False

  return JSONResponse({
    "status": "ok" if api_reachable else "degraded",
    "mcp_server": "ok",
    "api": {
      "url": API_BASE_URL,
      "openapi_url": API_OPENAPI_URL,
      "status": api_status,
      "reachable": api_reachable,
    },
    "openapi_tools": len(openapi_spec.get("paths", {})),
  })


if __name__ == "__main__":
  # Get the MCP HTTP app
  mcp_app = mcp.http_app()

  # Create main app with health check and MCP at root
  # FastMCP's http_app() already serves at /mcp, so mount at root
  # IMPORTANT: Pass lifespan from MCP app to initialize task groups
  app = Starlette(
    routes=[
      Route("/health", health_check),
      Mount("/", app=mcp_app),
    ],
    lifespan=mcp_app.lifespan,
  )
  app.add_middleware(AuthCapture)

  # Run server
  uvicorn.run(app, host="0.0.0.0", port=MCP_PORT)
