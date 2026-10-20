# Copyright 2026 KAITEN INC
# SPDX-License-Identifier: Apache-2.0

import pytest
from fastmcp.exceptions import ToolError

from kaiten_mcp import shapes


@pytest.mark.parametrize(
    ("used", "limit", "overage", "warning", "state"),
    [
        (3, 10, 0, None, "ok"),
        (8, 10, 0, 80, "warning"),
        (10, 10, 0, 80, "at_limit"),
        (11, 10, 20, None, "overage"),
        (13, 10, 20, None, "over"),
        (11, 10, 0, None, "over"),
        (999, -1, -1, None, "unlimited"),
    ],
)
def test_usage_state_follows_the_grant(used, limit, overage, warning, state):
    assert shapes.usage_state(used, limit, overage, warning)["state"] == state


def test_soft_limit_reports_its_hard_ceiling():
    out = shapes.usage_state(11, 10, 20)
    assert out["hard_ceiling"] == 12
    assert out["percent"] == 110.0


def test_a_warning_threshold_of_zero_means_none():
    assert shapes.usage_state(9, 10, 0, 0)["state"] == "ok"


def test_merge_map_sets_and_removes_keys():
    assert shapes.merge_map({"a": 1, "b": 2}, {"b": None, "c": 3}) == {"a": 1, "c": 3}


def test_changes_lists_only_what_differs():
    assert shapes.changes({"a": 1, "b": 2}, {"a": 1, "b": 3}) == {"b": {"from": 2, "to": 3}}


@pytest.mark.parametrize("bad", ["", "A", "-x", "x-", "has space", "x" * 101])
def test_slugs_are_constrained(bad):
    with pytest.raises(ToolError):
        shapes.slug(bad)


def test_parse_time_accepts_dates_and_timestamps():
    assert shapes.rfc3339(shapes.parse_time("2027-01-31")) == "2027-01-31T00:00:00Z"
    assert shapes.rfc3339(shapes.parse_time("2027-01-31T10:00:00+02:00")) == "2027-01-31T08:00:00Z"
