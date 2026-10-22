# Copyright 2026 KAITEN INC
# SPDX-License-Identifier: Apache-2.0

"""GET /health: the server's state, and whether Kaiten answers at KAITEN_API_BASE_URL.

The probe calls the gateway's /healthz without a token. A Community Edition gateway answers it
with 200; Kaiten Cloud's asks for a token and answers 401. Both mean Kaiten is there.
"""

from __future__ import annotations

import types

import httpx
import pytest
from starlette.testclient import TestClient

from kaiten_mcp import server

RealAsyncClient = httpx.AsyncClient


def _health(monkeypatch: pytest.MonkeyPatch, handler) -> dict:
    """GET /health, with the probe answered by `handler` instead of a real gateway."""
    fake = types.SimpleNamespace(
        AsyncClient=lambda **kw: RealAsyncClient(transport=httpx.MockTransport(handler), **kw),
        HTTPError=httpx.HTTPError,
    )
    monkeypatch.setattr(server, "httpx", fake)
    with TestClient(server.http_app(server.build())) as client:
        response = client.get("/health")
    assert response.status_code == 200
    return response.json()


@pytest.mark.parametrize(
    ("gateway", "reachable", "state"),
    [(200, True, "ok"), (401, True, "ok"), (503, False, "degraded")],
    ids=["community-edition", "kaiten-cloud", "unavailable"],
)
def test_the_probe_reads_the_gateway_answer(monkeypatch, gateway, reachable, state) -> None:
    body = _health(monkeypatch, lambda request: httpx.Response(gateway))
    assert body["status"] == state
    assert body["api"]["reachable"] is reachable
    assert body["api"]["status"] == gateway


def test_a_gateway_that_does_not_answer_is_degraded(monkeypatch) -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    body = _health(monkeypatch, refuse)
    assert body["status"] == "degraded"
    assert body["api"]["reachable"] is False
    assert "connection refused" in body["api"]["error"]
