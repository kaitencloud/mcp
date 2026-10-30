# Copyright 2026 KAITEN INC
# SPDX-License-Identifier: Apache-2.0

"""The webhook tools, driven through a real MCP client and the real SDK, over a mocked Kaiten Cloud."""

import json
import re

import httpx
import pytest
from kaitencloud.webhooks import WEBHOOK_EVENTS
from test_tools import call, fresh_client, kaiten  # noqa: F401  -- fresh_client is autouse

from kaiten_mcp.tools.webhooks import EVENTS, masked

WARNING = "com.kaiten.instance.entitlement.v1.usage_warning_threshold_reached"
CREATED = "com.kaiten.customer.v1.created"
# A Slack incoming-webhook URL: its path is the credential.
SLACK = "https://hooks.slack.com/services/T0001/B0001/XXXXsecretXXXX"
HOOK = {
    "id": "ep_1",
    "url": "https://hooks.example.com/kaiten",
    "eventTypes": [CREATED],
    "createdAt": "2026-10-30 10:00:00 +0000 UTC",
}


def cloud(routes: dict):
    """A Kaiten answering (method, path) with JSON, (status, JSON), or a function of the request.
    Anything else is a 404 from a route that is not served. Returns every request it got."""

    def handler(r: httpx.Request) -> httpx.Response:
        answer = routes.get((r.method, r.url.path.removeprefix("/api")))  # the SDK's /api prefix
        if callable(answer):
            answer = answer(r)
        if answer is None:
            answer = (404, {"status": 404, "code": "NOT_FOUND", "detail": "Not Found"})
        status, body = answer if isinstance(answer, tuple) else (200, answer)
        return httpx.Response(status, json=body)

    return kaiten(handler)


def test_the_catalogue_is_every_event_the_api_publishes():
    # Each type has the shape Kaiten Cloud accepts, and each event the SDK can parse is listed.
    shape = re.compile(
        r"^com\.kaiten\.[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)*\.v[1-9][0-9]*\.[a-z][a-z0-9_]*$"
    )
    assert len(EVENTS) == 116 and all(shape.match(t) for t in EVENTS)
    assert set(WEBHOOK_EVENTS) <= set(EVENTS)


async def test_an_agent_finds_the_event_without_reading_the_endpoints():
    seen = cloud({})
    out = (await call("kaiten_get_webhooks", events="warning threshold")).structured_content
    assert [e["type"] for e in out["events"]] == [WARNING]
    assert "early-warning" in out["events"][0]["fires"]
    assert seen == []


@pytest.mark.parametrize(
    ("status", "code", "says"),
    [
        (404, "NOT_FOUND", "self-hosting/consuming-events"),
        (403, "Webhooks.NotEntitled", "grants it no webhook events"),
        (503, "Webhooks.EntitlementVerificationUnavailable", "try again"),
    ],
    ids=["self-hosted", "not-entitled", "unverifiable"],
)
async def test_where_webhooks_are_not_served_the_agent_is_told_why(status, code, says):
    cloud({("GET", "/webhooks"): (status, {"status": status, "code": code, "detail": "no"})})
    for tool, args in (
        ("kaiten_get_webhooks", {}),
        ("kaiten_save_webhook", {"url": HOOK["url"], "event_types": [CREATED]}),
    ):
        result = await call(tool, **args)
        assert result.is_error and says in result.content[0].text


async def test_endpoints_come_with_their_deliveries_and_never_a_secret():
    cloud(
        {
            ("GET", "/webhooks"): [{**HOOK, "signingSecret": "whsec_never"}],
            ("GET", "/webhooks/history"): {
                "history": [
                    {"date": "2026-10-30T10:01:00Z", "hookId": "ep_1", "eventType": CREATED}
                    | {"status": "success", "responseStatusCode": 204},
                    {"date": "2026-10-30T10:02:00Z", "hookId": "ep_1", "eventType": CREATED}
                    | {"status": "fail", "responseStatusCode": 500, "responseStatusText": "boom"},
                ]
            },
        }
    )
    result = await call("kaiten_get_webhooks")
    [hook] = result.structured_content["webhooks"]
    assert hook["deliveries"] == {"success": 1, "fail": 1}
    assert [d["status"] for d in hook["recent"]] == ["fail", "success"]
    assert hook["recent"][0]["response_text"] == "boom"
    assert "whsec_never" not in json.dumps(result.structured_content)


async def test_a_new_endpoint_shows_its_secret_once():
    hooks: list[dict] = []

    def create(r):
        body = json.loads(r.content)
        hooks.append({"id": "ep_2", "createdAt": "now", **body})
        return 201, hooks[-1]

    def replace(r):
        hooks[0] = {**hooks[0], **json.loads(r.content)}
        return hooks[0]

    seen = cloud(
        {
            ("GET", "/webhooks"): lambda r: hooks,
            ("POST", "/webhooks"): create,
            ("GET", "/webhooks/ep_2"): lambda r: {**hooks[0], "signingSecret": "whsec_once"},
            ("PUT", "/webhooks/ep_2"): replace,
        }
    )
    args = {
        "url": "https://hooks.example.com/usage",
        "event_types": ["INSTANCE_ENTITLEMENT_USAGE_WARNING_THRESHOLD_REACHED"],
    }
    created = (await call("kaiten_save_webhook", **args)).structured_content
    assert created["status"] == "created" and created["webhook"]["signing_secret"] == "whsec_once"
    assert created["webhook"]["url"] == "https://hooks.example.com/…"
    assert "/usage" not in json.dumps(created)
    assert json.loads(seen[1].content) == {"url": args["url"], "eventTypes": [WARNING]}

    again = (await call("kaiten_save_webhook", **args)).structured_content
    assert again["status"] == "unchanged" and "signing_secret" not in again["webhook"]

    more = {**args, "event_types": [WARNING, "com.kaiten.instance.entitlement.v1.cap_exceeded"]}
    updated = (await call("kaiten_save_webhook", **more)).structured_content
    assert updated["status"] == "updated" and "signing_secret" not in updated["webhook"]
    assert updated["changes"]["event_types"]["from"] == [WARNING]
    assert len([r for r in seen if r.method == "POST"]) == 1


async def test_an_event_kaiten_does_not_publish_is_refused_before_any_call():
    seen = cloud({})
    result = await call(
        "kaiten_save_webhook",
        url="https://hooks.example.com/usage",
        event_types=["com.kaiten.instance.entitlement.v1.warning_threshold_reached"],
    )
    assert result.is_error and WARNING in result.content[0].text
    assert seen == []


async def test_a_deletion_is_previewed_first():
    seen = cloud({("GET", "/webhooks"): [HOOK], ("DELETE", "/webhooks/ep_1"): (204, None)})
    preview = (await call("kaiten_delete_webhook", webhook=HOOK["url"])).structured_content
    assert preview["status"] == "preview" and preview["webhook"]["event_types"] == [CREATED]
    assert not [r for r in seen if r.method == "DELETE"]
    deleted = (await call("kaiten_delete_webhook", webhook="ep_1", confirm=True)).structured_content
    assert deleted["status"] == "deleted"
    assert [r.url.path for r in seen if r.method == "DELETE"] == ["/api/webhooks/ep_1"]


def test_a_url_shows_its_scheme_and_host_only():
    assert masked(SLACK) == "https://hooks.slack.com/…"
    assert masked("https://user:pw@example.com:8443/hook?token=abc") == "https://example.com:8443/…"


async def test_no_answer_shows_a_webhook_path():
    hooks = [
        {"id": "ep_a", "url": SLACK, "eventTypes": [CREATED]},
        {"id": "ep_b", "url": SLACK.replace("B0001", "B0002"), "eventTypes": [WARNING]},
    ]
    attempt = {"date": "2026-10-30T10:01:00Z", "hookId": "ep_a", "hookUrl": SLACK}
    attempt |= {"eventType": CREATED, "status": "fail", "responseStatusCode": 404}
    attempt |= {"responseStatusText": "Cannot POST /services/T0001/B0001/XXXXsecretXXXX"}
    cloud(
        {
            ("GET", "/webhooks"): hooks,
            ("GET", "/webhooks/history"): {"history": [attempt]},
            ("DELETE", "/webhooks/ep_b"): (204, None),
        }
    )
    listed = (await call("kaiten_get_webhooks")).structured_content
    # Two endpoints on one host read the same: their ids tell them apart.
    assert [(h["id"], h["url"]) for h in listed["webhooks"]] == [
        ("ep_a", "https://hooks.slack.com/…"),
        ("ep_b", "https://hooks.slack.com/…"),
    ]
    assert listed["webhooks"][0]["recent"][0]["response_text"] == "Cannot POST …"
    # The full URL still finds its endpoint, and the answer still hides it.
    preview = (await call("kaiten_delete_webhook", webhook=hooks[1]["url"])).structured_content
    assert preview["webhook"]["id"] == "ep_b"
    deleted = (await call("kaiten_delete_webhook", webhook="ep_b", confirm=True)).structured_content
    missing = await call("kaiten_delete_webhook", webhook=SLACK + "-gone")
    assert missing.is_error and "https://hooks.slack.com/…" in missing.content[0].text
    for answer in (
        json.dumps(listed),
        json.dumps(preview),
        json.dumps(deleted),
        missing.content[0].text,
    ):
        assert "T0001" not in answer and "XXXX" not in answer
