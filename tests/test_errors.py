# Copyright 2026 KAITEN INC
# SPDX-License-Identifier: Apache-2.0

"""What an agent reads, and what the server logs, when a tool fails."""

import logging

import httpx
import pytest
from test_tools import call, fresh_client, kaiten  # noqa: F401  -- fresh_client is autouse

REFUSAL = {
    "status": 422,
    "code": "UpdateCustomer.Invalid",
    "detail": "the customer is invalid",
    "errors": [{"location": "body.domain", "message": "not a domain"}],
    "errorId": "err-42",
}


@pytest.fixture
def logged(caplog):
    """FastMCP's own log records: its logger stops at "fastmcp", short of the root caplog listens on."""
    logger = logging.getLogger("fastmcp")
    logger.addHandler(caplog.handler)
    yield caplog  # its records are the current test phase's
    logger.removeHandler(caplog.handler)


async def test_a_refusal_reaches_the_agent_whole():
    kaiten(lambda r: httpx.Response(422, json=REFUSAL))
    result = await call("kaiten_save_customer", customer="acme", domain="nope")
    assert result.is_error
    assert result.content[0].text == (
        "Kaiten API 422 UpdateCustomer.Invalid: the customer is invalid (errorId: err-42)\n"
        "  - body.domain: not a domain"
    )


async def test_a_refusal_is_logged_on_one_line(logged):
    kaiten(lambda r: httpx.Response(422, json=REFUSAL))
    await call("kaiten_save_customer", customer="acme", domain="nope")
    assert [r.getMessage() for r in logged.records] == ["Error calling tool 'kaiten_save_customer'"]
    assert not logged.records[0].exc_info  # no traceback


async def test_an_unexpected_error_keeps_its_traceback(logged):
    # A 200 that is no customer: the SDK cannot read it, and that is no refusal.
    kaiten(lambda r: httpx.Response(200, json={"unexpected": True}))
    result = await call("kaiten_save_customer", customer="acme", domain="acme.io")
    assert result.is_error and result.content[0].text.startswith("Error calling tool")
    [record] = [
        r for r in logged.records if r.getMessage() == "Error calling tool 'kaiten_save_customer'"
    ]
    assert record.exc_info  # its traceback


async def test_a_tool_keeps_its_own_explanation_of_a_refusal():
    kaiten(
        lambda r: httpx.Response(
            404, json={"status": 404, "code": "Customer.NotFound", "detail": "no such customer"}
        )
    )
    result = await call("kaiten_get_customer", customer="ghost")
    assert result.content[0].text == (
        "No customer 'ghost' found (no such customer). Use kaiten_search to find the right slug."
    )
