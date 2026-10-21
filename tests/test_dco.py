# Copyright 2026 KAITEN INC
# SPDX-License-Identifier: Apache-2.0

"""Exercise the actual inline DCO workflow without credentials or API calls."""

import textwrap
from pathlib import Path

import pytest

WORKFLOW = Path(__file__).resolve().parents[1] / ".github/workflows/dco.yml"


@pytest.fixture
def dco():
    script = textwrap.dedent(WORKFLOW.read_text().split("        run: |\n", 1)[1])
    namespace = {"__name__": "dco_test"}
    exec(compile(script, str(WORKFLOW), "exec"), namespace)
    return namespace


def commit(message, *, merge=False, bot=False):
    return {
        "sha": "abc123",
        "parents": [{"sha": "parent"}] * (2 if merge else 1),
        "author": {"type": "Bot" if bot else "User"},
        "commit": {"author": {"name": "Jane Doe", "email": "jane@example.com"}, "message": message},
    }


@pytest.mark.parametrize("merge", [False, True])
def test_unsigned_commits_fail_including_merges(dco, merge):
    assert "no Signed-off-by" in dco["problem"](commit("Change", merge=merge))


@pytest.mark.parametrize("merge", [False, True])
def test_matching_author_signoff_passes(dco, merge):
    message = "Change\n\nSigned-off-by: Jane Doe <jane@example.com>\n"
    assert dco["problem"](commit(message, merge=merge)) is None


def test_other_authors_signoff_fails(dco):
    message = "Change\n\nSigned-off-by: Other Person <other@example.com>\n"
    assert "not by its author" in dco["problem"](commit(message))


def test_body_text_is_not_a_trailer(dco):
    message = "Change\n\nSigned-off-by: Jane Doe <jane@example.com>\n\nUnrelated body text.\n"
    assert "no Signed-off-by" in dco["problem"](commit(message))


def test_bot_exemption(dco):
    assert dco["problem"](commit("Dependency update", bot=True)) is None


def test_over_250_commits_fails_before_api_call(dco, monkeypatch, capsys):
    monkeypatch.setenv("PR_COMMIT_COUNT", "251")
    dco["commits"] = lambda: pytest.fail("The capped endpoint must not be called")
    assert dco["main"]() == 1
    assert "over 250 commits" in capsys.readouterr().out


def test_incomplete_commit_list_fails(dco, monkeypatch, capsys):
    monkeypatch.setenv("PR_COMMIT_COUNT", "2")
    dco["commits"] = lambda: []
    assert dco["main"]() == 1
    assert "incomplete list" in capsys.readouterr().out


def test_complete_signed_commit_list_passes(dco, monkeypatch):
    monkeypatch.setenv("PR_COMMIT_COUNT", "1")
    dco["commits"] = lambda: [commit("Change\n\nSigned-off-by: Jane Doe <jane@example.com>\n")]
    assert dco["main"]() == 0
