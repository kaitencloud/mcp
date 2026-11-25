# Copyright 2026 KAITEN INC
# SPDX-License-Identifier: Apache-2.0

"""An instance's metadata, changed key by key.

Driven through a real MCP client and the real SDK, over a mocked Kaiten that holds one instance.
A Kaiten whose PATCH /instances/{slug} takes metadata merges it on its side; one whose PATCH does
not refuses the member, and the tool then writes the merged metadata back whole with a PUT.
"""

import json

import pytest
from test_billing import api, sent
from test_tools import INSTANCE, call, fresh_client  # noqa: F401  -- fresh_client is autouse

HELD = {"region": "eu-west", "tier": "gold", "owner": "ops"}

# What a Kaiten whose PATCH body has no metadata member answers one: the body's schema refuses
# it before anything is applied.
NO_METADATA_MEMBER = (
    422,
    {
        "status": 422,
        "code": "UNPROCESSABLE",
        "detail": "validation failed",
        "errors": [{"location": "body.metadata", "message": "unexpected property", "value": {}}],
    },
)


def tako(*, merges: bool, meanwhile: dict | None = None) -> tuple[list, dict]:
    """tako-prod, holding HELD as metadata, on a Kaiten whose PATCH merges metadata or not, and
    where another caller sets the `meanwhile` keys right after the first read. Returns the
    requests it got, and the instance as it holds it."""
    state = {**INSTANCE, "metadata": dict(HELD)}
    reads = []

    def get(r):
        answer = json.loads(json.dumps(state))
        if not reads and meanwhile:
            state["metadata"].update(meanwhile)
        reads.append(r)
        return answer

    def patch(r):
        body = json.loads(r.content)
        if "metadata" in body:
            if not merges:
                return NO_METADATA_MEMBER
            for key, value in body["metadata"].items():
                if value is None:
                    state["metadata"].pop(key, None)
                else:
                    state["metadata"][key] = value
        if "status" in body:
            state["status"] = body["status"]
        if "lifecycleStage" in body:
            state["lifecycleStage"] = body["lifecycleStage"]
        return 204, None

    def put(r):
        body = json.loads(r.content)
        state.update(
            name=body["name"],
            description=body.get("description", ""),
            licenseId=body["licenseId"],
            startLicenseDate=body["startLicenseDate"],
            endLicenseDate=body["endLicenseDate"],
            metadata=body.get("metadata") or {},
        )
        return 204, None

    seen = api(
        {
            ("GET", "/instances/tako-prod"): get,
            ("PATCH", "/instances/tako-prod"): patch,
            ("PUT", "/instances/tako-prod"): put,
            # The answer's summary reads the catalogue and the usage; empty ones will do here.
            ("GET", "/licenses"): [],
            ("GET", "/entitlements"): [],
            ("GET", "/license-families"): [],
            ("GET", "/instances/tako-prod/entitlements/usage"): [],
        }
    )
    return seen, state


async def update(**args) -> dict:
    result = await call("kaiten_update_instance", instance="tako-prod", **args)
    assert not result.is_error, result.content[0].text
    return result.structured_content


async def test_metadata_keys_are_merged_by_kaiten():
    seen, state = tako(merges=True)
    out = await update(metadata={"tier": "platinum", "owner": None, "seats": 40})
    # Only the keys passed travel, the one to remove as null; nothing is written back whole.
    assert sent(seen, "PATCH", "instances/tako-prod") == [
        {"metadata": {"tier": "platinum", "owner": None, "seats": 40}}
    ]
    assert sent(seen, "PUT", "instances/tako-prod") == []
    assert state["metadata"] == {"region": "eu-west", "tier": "platinum", "seats": 40}
    assert out["status"] == "updated"
    assert out["changes"]["metadata"]["to"] == state["metadata"]
    assert not out.get("notes")


@pytest.mark.parametrize("merges", [True, False], ids=["merge", "no-merge"])
async def test_a_key_another_caller_sets_meanwhile(merges):
    # Another caller sets a key between the tool's read and its write: the merge keeps it, and
    # only a Kaiten without the merge loses it, as the answer's note says.
    _, state = tako(merges=merges, meanwhile={"contact": "sakura@example.com"})
    out = await update(metadata={"tier": "platinum"})
    kept = {**HELD, "tier": "platinum", "contact": "sakura@example.com"}
    assert state["metadata"] == (kept if merges else {**HELD, "tier": "platinum"})
    assert bool(out.get("notes")) is not merges


async def test_without_the_merge_the_metadata_is_written_back_whole():
    seen, state = tako(merges=False)
    out = await update(metadata={"tier": "platinum", "owner": None})
    # The merge is asked for first; refused, the tool merges what it read and writes it back.
    assert [r.method for r in seen if r.method in ("PATCH", "PUT")] == ["PATCH", "PUT"]
    assert sent(seen, "PUT", "instances/tako-prod")[0]["metadata"] == {
        "region": "eu-west",
        "tier": "platinum",
    }
    assert state["metadata"] == {"region": "eu-west", "tier": "platinum"}
    assert out["status"] == "updated"
    assert any("does not merge metadata" in n for n in out["notes"])


async def test_without_the_merge_a_change_that_changes_nothing_writes_nothing():
    seen, _ = tako(merges=False)
    out = await update(metadata={"tier": "gold", "gone": None})
    assert sent(seen, "PUT", "instances/tako-prod") == []
    assert out["status"] == "unchanged" and not out.get("notes")


@pytest.mark.parametrize("merges", [True, False], ids=["merge", "no-merge"])
async def test_other_fields_and_metadata_change_together(merges):
    seen, state = tako(merges=merges)
    out = await update(name="Tako Prod", metadata={"tier": "platinum"}, status="DEGRADED")
    puts = sent(seen, "PUT", "instances/tako-prod")
    # The PUT the name needs restates the metadata as read, and comes before the merge.
    assert puts[0]["name"] == "Tako Prod" and puts[0]["metadata"] == HELD
    assert len(puts) == (1 if merges else 2)
    assert state["metadata"] == {**HELD, "tier": "platinum"}
    assert (state["name"], state["status"]) == ("Tako Prod", "DEGRADED")
    assert set(out["changes"]) == {"name", "metadata", "status"}


async def test_a_metadata_refusal_stands_as_kaiten_gave_it():
    refusal = (
        422,
        {
            "status": 422,
            "code": "MetadataValidation.Failed",
            "detail": "metadata does not match the declared fields",
            "errors": [{"location": "metadata.seats", "message": "expected integer"}],
        },
    )
    api(
        {
            ("GET", "/instances/tako-prod"): {**INSTANCE, "metadata": HELD},
            ("PATCH", "/instances/tako-prod"): refusal,
        }
    )
    result = await call("kaiten_update_instance", instance="tako-prod", metadata={"seats": "x"})
    assert result.is_error
    assert "MetadataValidation.Failed" in result.content[0].text


async def test_no_metadata_no_patch_of_it():
    seen, _ = tako(merges=True)
    out = await update(status="DEGRADED")
    assert sent(seen, "PATCH", "instances/tako-prod") == [{"status": "DEGRADED"}]
    assert out["changes"] == {"status": {"from": None, "to": "DEGRADED"}}
