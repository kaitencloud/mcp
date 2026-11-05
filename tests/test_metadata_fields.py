# Copyright 2026 KAITEN INC
# SPDX-License-Identifier: Apache-2.0

"""Where a metadata field is shown: its position among its resource type's active fields.

Driven through a real MCP client and the real SDK, over a mocked Kaiten whose answers are whole.
"""

import json

import pytest
from test_billing import api, sent
from test_tools import call, fresh_client  # noqa: F401  -- fresh_client is autouse

STAMPS = {"createdAt": "2026-11-01T00:00:00Z", "updatedAt": "2026-11-01T00:00:00Z"}


def field(key: str, order: int) -> dict:
    """An INSTANCE metadata field, as Kaiten answers it."""
    return {
        "id": f"id-{key}",
        "key": key,
        "label": key.title(),
        "jsonSchema": {"type": "string"},
        "resourceType": "INSTANCE",
        "displayOrder": order,
        "createdBy": {"id": "u", "name": "u"},
        "updatedBy": {"id": "u", "name": "u"},
        **STAMPS,
    }


def fields(*held: dict):
    """A Kaiten holding these fields, created in this order. It lists them by stored order, then by
    creation, and a reorder sets each listed id's order to its index, as Kaiten does."""
    state = list(held)

    def create(r):
        body = json.loads(r.content)
        state.append(field(body["key"], body.get("displayOrder", 0)))
        return 201, state[-1]

    def reorder(r):
        for i, fid in enumerate(json.loads(r.content)["ids"]):
            next(f for f in state if f["id"] == fid)["displayOrder"] = i
        return 204, None

    return api(
        {
            ("GET", "/metadata-fields"): lambda r: sorted(state, key=lambda f: f["displayOrder"]),
            ("POST", "/metadata-fields"): create,
            ("POST", "/metadata-fields/reorder"): reorder,
        }
    )


def moved(*moves: tuple[str, int, int]) -> dict:
    return {key: {"from": a, "to": b} for key, a, b in moves}


@pytest.mark.parametrize(
    ("orders", "key", "position", "ids", "order"),
    [
        (
            (0, 1, 2),
            "zone",
            0,
            ["id-zone", "id-region", "id-tier"],
            moved(("zone", 2, 0), ("region", 0, 1), ("tier", 1, 2)),
        ),
        ((0, 1, 2), "tier", 1, None, None),
        ((0, 1, 2), "zone", 9, None, None),
        # An archive and a re-create leave two fields at 0: listed right, stored wrong.
        (
            (0, 0, 1),
            "tier",
            1,
            ["id-region", "id-tier", "id-zone"],
            moved(("tier", 0, 1), ("zone", 1, 2)),
        ),
    ],
    ids=["move", "no-op", "past the end is last", "duplicate orders"],
)
async def test_a_field_is_put_at_its_position_and_the_order_rewritten_only_when_it_differs(
    orders, key, position, ids, order
):
    seen = fields(*(field(k, o) for k, o in zip(("region", "tier", "zone"), orders, strict=True)))
    out = (await call("kaiten_save_metadata_field", key=key, position=position)).structured_content
    assert sent(seen, "POST", "metadata-fields/reorder") == ([{"ids": ids}] if ids else [])
    assert out["status"] == ("updated" if order else "unchanged")
    assert out.get("changes", {}).get("order") == order
    assert out["metadata_field"]["order"] == min(position, 2)


async def test_a_field_is_created_at_its_position():
    seen = fields(field("region", 0), field("tier", 1))
    out = (
        await call(
            "kaiten_save_metadata_field",
            key="plan_code",
            label="Plan code",
            json_schema={"type": "string"},
            position=0,
        )
    ).structured_content
    assert sent(seen, "POST", "metadata-fields")[0] == {
        "resourceType": "INSTANCE",
        "key": "plan_code",
        "label": "Plan code",
        "jsonSchema": {"type": "string"},
    }
    assert sent(seen, "POST", "metadata-fields/reorder") == [
        {"ids": ["id-plan_code", "id-region", "id-tier"]}
    ]
    assert out["status"] == "created"
    assert out["metadata_field"]["order"] == 0
    assert out["changes"] == {"order": moved(("region", 0, 1), ("tier", 1, 2))}


async def test_an_archived_field_has_no_position():
    seen = fields(field("region", 0))
    out = await call("kaiten_save_metadata_field", key="region", archived=True, position=0)
    assert out.is_error and "An archived field has no position" in out.content[0].text
    assert seen == []
