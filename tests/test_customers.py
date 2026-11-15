# Copyright 2026 KAITEN INC
# SPDX-License-Identifier: Apache-2.0

"""A customer's billing e-mail: where invoices sent to it go.

Driven through a real MCP client and the real SDK, over a mocked Kaiten whose answers are whole.
"""

import json

import pytest
from test_billing import api, sent
from test_tools import CUSTOMER, call, fresh_client  # noqa: F401  -- fresh_client is autouse

EMAIL = "billing@acme.com"


def acme(email: str | None = None) -> list:
    """acme, holding `email` as its billing e-mail. Its PUT replaces it as Kaiten does: the domain
    and the external id in full, the billing e-mail only when sent, and "" removes it."""
    state = {**CUSTOMER, **({"billingEmail": email} if email else {})}

    def put(r):
        body = json.loads(r.content)
        state.update(
            name=body["name"],
            domain=body.get("domain"),
            externalCustomerId=body.get("externalCustomerId"),
        )
        if "billingEmail" in body:
            state["billingEmail"] = body["billingEmail"] or None
        return 204, None

    return api({("GET", "/customers/acme"): lambda r: state, ("PUT", "/customers/acme"): put})


async def test_a_customer_is_created_with_its_billing_email():
    seen = api({("POST", "/customers"): lambda r: (201, {**CUSTOMER, **json.loads(r.content)})})
    out = (
        await call("kaiten_save_customer", customer="acme", name="Acme", billing_email=EMAIL)
    ).structured_content
    assert sent(seen, "POST", "customers") == [
        {"name": "Acme", "slug": "acme", "billingEmail": EMAIL}
    ]
    assert (out["status"], out["customer"]["billing_email"]) == ("created", EMAIL)


@pytest.mark.parametrize(
    ("held", "args", "put", "changes"),
    [
        (
            None,
            {"billing_email": EMAIL},
            {"domain": "acme.com", "billingEmail": EMAIL},
            {"billing_email": {"from": None, "to": EMAIL}},
        ),
        (
            EMAIL,
            {"billing_email": ""},
            {"domain": "acme.com", "billingEmail": ""},
            {"billing_email": {"from": EMAIL, "to": None}},
        ),
        # Left out, it is not sent, and Kaiten keeps it.
        (
            EMAIL,
            {"domain": "acme.io"},
            {"domain": "acme.io"},
            {"domain": {"from": "acme.com", "to": "acme.io"}},
        ),
    ],
    ids=["set", "removed with ''", "kept when left out"],
)
async def test_a_billing_email_is_set_removed_or_kept(held, args, put, changes):
    seen = acme(held)
    out = (await call("kaiten_save_customer", customer="acme", **args)).structured_content
    assert sent(seen, "PUT", "customers/acme") == [{"name": "Acme", **put}]
    assert (out["status"], out["changes"]) == ("updated", changes)
    after = changes.get("billing_email", {"to": held})["to"]
    assert out["customer"].get("billing_email") == after


@pytest.mark.parametrize(
    ("held", "email"), [(EMAIL, EMAIL), (None, "")], ids=["the same address", "none, removed"]
)
async def test_a_billing_email_already_held_writes_nothing(held, email):
    seen = acme(held)
    out = (
        await call("kaiten_save_customer", customer="acme", billing_email=email)
    ).structured_content
    assert out["status"] == "unchanged"
    assert [r.method for r in seen] == ["GET"]


async def test_a_customer_reads_with_its_billing_email():
    api(
        {
            ("GET", "/customers/acme"): {**CUSTOMER, "billingEmail": EMAIL},
            ("GET", "/instances"): [],
            ("GET", "/licenses"): [],
            ("GET", "/entitlements"): [],
            ("GET", "/license-families"): [],
        }
    )
    out = (await call("kaiten_get_customer", customer="acme")).structured_content
    assert out["customer"] == {
        "slug": "acme",
        "name": "Acme",
        "domain": "acme.com",
        "billing_email": EMAIL,
    }
