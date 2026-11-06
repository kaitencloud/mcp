# Copyright 2026 KAITEN INC
# SPDX-License-Identifier: Apache-2.0

"""Usage reports under an idempotency key, the usage history, usage windows, and unlimited grants.

Driven through a real MCP client and the real SDK, over a mocked Kaiten whose answers are whole.
"""

import json

import httpx
import pytest
from test_billing import api, problem
from test_plans import family, version
from test_tools import (  # noqa: F401  -- fresh_client is autouse
    EXCEPTION,
    STAMPS,
    call,
    drivers,
    fresh_client,
    tako,
)

from kaiten_mcp.tools import usage as usage_tools

USAGE = "/instances/tako-prod/entitlements/delivery-drivers/usage"
KEY = "llm-call-9f2c:tokens"
WINDOW = ("2026-11-01T00:00:00Z", "2026-12-01T00:00:00Z")  # period_start, resets_at
NOVEMBER = {"currentPeriodStart": WINDOW[0], "currentPeriodEnd": WINDOW[1]}
OCTOBER = {"currentPeriodStart": "2026-10-01T00:00:00Z", "currentPeriodEnd": WINDOW[0]}


def reported(used: float, key: str | None, **extra) -> dict:
    """A report's answer: the usage it leaves this November, and the key Kaiten accepted it under."""
    return {**drivers(used, 15, EXCEPTION), **NOVEMBER, "transactionId": key, **extra}


def reporting(post) -> list[httpx.Request]:
    """tako-prod, with 8 of 15 drivers used this November, whose usage reports `post` answers."""
    return api({**tako({**drivers(8, 15, EXCEPTION), **NOVEMBER}, grant=10), ("POST", USAGE): post})


def report(**args):
    return call("kaiten_report_usage", instance="tako-prod", entitlement="delivery-drivers", **args)


def posted(seen: list[httpx.Request]) -> httpx.Request:
    return next(r for r in seen if r.method == "POST")


async def test_a_report_sends_its_idempotency_key_and_says_what_kaiten_counted():
    seen = reporting(lambda r: httpx.Response(200, json=reported(12, KEY)))
    out = (
        await report(amount=4, behavior="append", metadata={"model": "small"}, idempotency_key=KEY)
    ).structured_content
    assert posted(seen).headers["idempotency-key"] == KEY
    assert json.loads(posted(seen).content) == {
        "value": {"type": "number", "value": 4},
        "behavior": "append",
        "metadata": {"model": "small"},
    }
    assert out["usage"]["used"] == 12
    assert {k: out[k] for k in ("status", "before", "transaction_id", "replayed")} == {
        "status": "recorded",
        "before": 8,
        "transaction_id": KEY,
        "replayed": False,
    }
    assert (out["period_start"], out["resets_at"]) == WINDOW

    # Without a key none is sent, and Kaiten names none.
    seen = reporting(lambda r: httpx.Response(200, json=reported(12, None)))
    out = (await report(amount=4, behavior="append")).structured_content
    assert "idempotency-key" not in posted(seen).headers
    assert json.loads(posted(seen).content) == {
        "value": {"type": "number", "value": 4},
        "behavior": "append",
    }
    assert (out["status"], out["replayed"], "transaction_id" in out) == ("recorded", False, False)


async def test_a_replayed_report_counts_nothing_and_shows_where_usage_stands():
    # Kaiten answers a replay with the first report's answer, which counted in October.
    first = reported(12, KEY, provenance=None, source=None, **OCTOBER)
    replay = httpx.Response(200, json=first, headers={"Idempotent-Replayed": "true"})
    seen = reporting(lambda r: replay)
    out = (await report(amount=4, behavior="append", idempotency_key=KEY)).structured_content
    assert posted(seen).headers["idempotency-key"] == KEY
    assert (out["status"], out["replayed"], out["transaction_id"]) == ("unchanged", True, KEY)
    # Where the instance stands is the usage read before the report, in November's window.
    assert out["usage"]["used"] == 8
    assert (out["period_start"], out["resets_at"]) == WINDOW
    assert out["notes"] == [
        "Kaiten had already recorded this report under its idempotency_key: nothing was counted again"
    ]


async def test_metadata_kaiten_did_not_store_is_said():
    dropped = httpx.Response(
        200, json=reported(12, None), headers={"Kaiten-Metadata-Dropped": "too_large"}
    )
    reporting(lambda r: dropped)
    out = (await report(amount=4, behavior="append", metadata={"log": "x"})).structured_content
    assert out["status"] == "recorded"
    assert "its metadata is over 4 KiB and was not stored; the report counted" in out["notes"]


@pytest.mark.parametrize(
    ("code", "note"),
    [
        (
            "ReportEntitlementUsageMetric.TransactionIdReused",
            f"idempotency_key '{KEY}' was already spent on a report of another behavior or amount: "
            "nothing was recorded.",
        ),
        (
            "ReportEntitlementUsageMetric.ThresholdExceeded",
            "The report was refused and not recorded.",
        ),
    ],
    ids=["key spent on another report", "past the limit"],
)
async def test_a_refused_report_is_rejected_and_says_why(code, note):
    reporting(lambda r: problem(409, code, "refused"))
    out = (await report(amount=40, behavior="append", idempotency_key=KEY)).structured_content
    assert (out["status"], out["reason"]) == ("rejected", f"{code}: refused")
    assert out["notes"][0].startswith(note)
    assert out["usage"]["used"] == 8


@pytest.mark.parametrize(
    ("tool", "args"),
    [
        ("kaiten_report_usage", {"amount": 1, "behavior": "append"}),
        ("kaiten_get_usage_reports", {}),
    ],
)
async def test_an_idempotency_key_is_checked_before_any_call(tool, args):
    seen = reporting(lambda r: httpx.Response(200, json=reported(12, KEY)))
    out = await call(
        tool,
        instance="tako-prod",
        entitlement="delivery-drivers",
        idempotency_key="llm call 9f2c",
        **args,
    )
    assert out.is_error
    assert "'llm call 9f2c' is not a Kaiten idempotency key" in out.content[0].text
    assert seen == []


def entry(seq: int, amount: int, before: int, key: str | None = None, **extra) -> dict:
    """A report as tako-prod's usage history of delivery-drivers keeps it."""
    return {
        "aggregationMethod": "SUM",
        "behavior": "append",
        "delta": str(amount),
        "entitlementId": "e1",
        "eventCountAfter": seq,
        "instanceId": "i1",
        "licenseId": "l1",
        "limitValue": "15",
        "overageDelta": "0",
        "overagePercent": 20,
        "properties": None,
        "reportSeq": seq,
        "reportedAt": f"2026-11-0{seq}T10:00:00Z",
        "reportedValue": str(amount),
        "transactionId": key,
        "valueAfter": str(before + amount),
        "valueBefore": str(before),
        "windowEnd": "2026-12-01T00:00:00Z",
        "windowStart": "2026-11-01T00:00:00Z",
        **extra,
    }


HISTORY = [
    entry(1, 3, 0),
    entry(2, 4, 3, KEY, properties={"model": "small"}),
    entry(3, 1, 7, limitValue=None, overagePercent=None),
]


def a_history() -> list[httpx.Request]:
    """HISTORY, oldest first and two reports a page, filtered by transactionId as Kaiten does."""

    def page(r: httpx.Request) -> dict:
        key, after = r.url.params.get("transactionId"), int(r.url.params.get("afterSeq", 0))
        rows = [e for e in HISTORY if e["reportSeq"] > after and key in (None, e["transactionId"])]
        return {"items": rows[:2], "nextAfterSeq": rows[1]["reportSeq"] if len(rows) > 2 else None}

    return api({("GET", f"{USAGE}/reports"): page})


async def test_the_usage_history_reads_newest_first():
    seen = a_history()
    out = (
        await call(
            "kaiten_get_usage_reports",
            instance="tako-prod",
            entitlement="delivery-drivers",
            limit=2,
        )
    ).structured_content
    assert out == {
        "instance": "tako-prod",
        "entitlement": "delivery-drivers",
        "reports": [
            {
                "at": "2026-11-03T10:00:00Z",
                "behavior": "append",
                "amount": "1",
                "before": "7",
                "after": "8",
                "limit": "unlimited",
            },
            {
                "at": "2026-11-02T10:00:00Z",
                "behavior": "append",
                "amount": "4",
                "before": "3",
                "after": "7",
                "limit": "15",
                "transaction_id": KEY,
                "metadata": {"model": "small"},
            },
        ],
        "more": True,
    }
    # Kaiten's last 30 days, walked page by page.
    assert [dict(r.url.params) for r in seen] == [
        {"limit": "500"},
        {"afterSeq": "2", "limit": "500"},
    ]

    # The report sent under a key, from a date on.
    out = (
        await call(
            "kaiten_get_usage_reports",
            instance="tako-prod",
            entitlement="delivery-drivers",
            idempotency_key=KEY,
            since="2026-11-01",
        )
    ).structured_content
    assert [(r["at"], r["transaction_id"]) for r in out["reports"]] == [
        ("2026-11-02T10:00:00Z", KEY)
    ]
    assert "more" not in out
    assert dict(seen[-1].url.params) == {
        "from": "2026-11-01T00:00:00Z",
        "transactionId": KEY,
        "limit": "500",
    }


async def test_a_history_past_the_walk_says_its_latest_reports_may_be_missing(monkeypatch):
    monkeypatch.setattr(usage_tools, "WALK", 2)
    a_history()
    out = (
        await call("kaiten_get_usage_reports", instance="tako-prod", entitlement="delivery-drivers")
    ).structured_content
    assert [r["at"] for r in out["reports"]] == ["2026-11-02T10:00:00Z", "2026-11-01T10:00:00Z"]
    assert out["notes"][0].startswith("only the first 2 reports of the range were read")


async def test_usage_shows_the_window_it_counts_in():
    # A BILLING_PERIOD window is checked against the subscription's period by both its ends.
    api(tako({**drivers(8, 15, EXCEPTION), **NOVEMBER}, grant=10))
    row = (await call("kaiten_get_usage")).structured_content["usage"][0]
    assert (row["period_start"], row["resets_at"]) == WINDOW
    one = (
        await call("kaiten_get_instance", instance="tako-prod", recent_events=0)
    ).structured_content
    assert one["limits"][0]["period_start"] == WINDOW[0]

    # A lifetime counter has no window.
    api(tako(drivers(8, 15, EXCEPTION), grant=10))
    row = (await call("kaiten_get_usage")).structured_content["usage"][0]
    assert "period_start" not in row and "resets_at" not in row


async def test_an_unlimited_grant_shows_what_kaiten_stores():
    unlimited = {
        "entitlementSlug": "delivery-drivers",
        "entitlementName": "Drivers",
        "entitlementType": "NUMBER",
        "licenseId": "id-growth",
        "licenseSlug": "growth",
        "value": {"type": "number", "value": -1},
        "limitCapExceededOveragePercent": -1,
        "createdBy": {"id": "u", "name": "u"},
        "updatedBy": {"id": "u", "name": "u"},
        **STAMPS,
    }
    api(
        {
            ("GET", "/instances"): [],
            ("GET", "/license-families/growth"): family(version("growth", isDefault=True)),
            ("GET", "/licenses/growth/entitlements"): [unlimited],
        }
    )
    one = (await call("kaiten_get_catalog", plan="growth")).structured_content
    assert one["versions"][0]["grants"] == [
        {
            "entitlement": "delivery-drivers",
            "type": "NUMBER",
            "value": "unlimited",
            "raw_value": -1,
            "overage_percent": -1,
        }
    ]

    # Usage measured against it says the same.
    api(tako({**drivers(8, -1, {}), "limitCapExceededOveragePercent": -1}, grant=-1))
    row = (await call("kaiten_get_usage")).structured_content["usage"][0]
    assert {k: row[k] for k in ("limit", "raw_limit", "overage_percent", "state")} == {
        "limit": "unlimited",
        "raw_limit": -1,
        "overage_percent": -1,
        "state": "unlimited",
    }
