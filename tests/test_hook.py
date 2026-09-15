"""Rule 12: the opening post is a link-free one-claim hook (draft/hook.py)."""

from __future__ import annotations

import pytest

from draft.drafter import check_hard_rules
from draft.hook import HOOK_MAX_CHARS, hook_problems, hook_rule
from draft.schema import Draft
from swarm.cells import cell_problems
from swarm.genome import CLOSER, HOOK

URL = "https://example.org/study"


def test_clean_hook_passes():
    assert (
        hook_problems("The armoring worked in blood. It never showed up in tumor.", url=URL) == []
    )


@pytest.mark.parametrize(
    "text",
    [
        "1/6 The armoring worked in blood.",
        "The armoring worked in blood. (1/n)",
        "The armoring worked in blood. \U0001f9f5",
        "A thread on why the armoring only worked in blood.",
        "The armoring worked in blood. Details below.",
    ],
)
def test_position_markers_and_throat_clearing_fail(text):
    assert any("first post contains" in p for p in hook_problems(text, url=URL))


def test_link_in_the_opener_fails():
    problems = hook_problems(f"The armoring worked in blood. {URL}", url=URL)
    assert any("source URL" in p for p in problems)
    assert any("last post" in p for p in problems)


def test_any_other_link_in_the_opener_fails():
    assert hook_problems("Worth reading: https://elsewhere.test/x", url=URL) == [
        "first post contains a link; links belong in the last post"
    ]


def test_opener_longer_than_the_cap_fails():
    long = "The armoring worked in blood but not in tumor. " * 8
    assert any("not a summary" in p for p in hook_problems(long, url=URL))
    assert len(long) > HOOK_MAX_CHARS


def test_a_single_post_carries_the_url_and_is_not_capped():
    long = f"The armoring worked in blood but not in tumor. {'Detail. ' * 30}{URL}"
    assert hook_problems(long, url=URL, carries_url=True) == []


def test_check_hard_rules_applies_the_hook_rule_to_a_thread():
    draft = Draft(
        thread=[f"1/3 Armoring worked in blood. {URL}", "The mechanism.", f"Takeaway. {URL}"],
        why_it_matters="",
        suggested_visual="",
    )
    problems = check_hard_rules(draft, url=URL, source="pubmed")
    assert any("position marker" in p for p in problems)
    assert any("belongs in the last post" in p for p in problems)


def test_check_hard_rules_leaves_a_single_post_alone():
    draft = Draft(
        thread=[f"Armoring worked in blood, not in tumor. {URL}"],
        why_it_matters="",
        suggested_visual="",
    )
    assert check_hard_rules(draft, url=URL, source="pubmed") == []


def test_hook_cell_is_checked_and_the_closer_is_not():
    kwargs = dict(source_text="", url=URL, is_preprint=False)
    assert any(
        "position marker" in p
        for p in cell_problems("1/6 Armoring worked in blood.", slot=HOOK, **kwargs)
    )
    assert cell_problems(f"Takeaway. {URL}", slot=CLOSER, **kwargs) == []


def test_hook_cell_that_must_carry_the_url_is_exempt():
    text = f"Armoring worked in blood, not in tumor. {URL}"
    assert (
        cell_problems(text, slot=HOOK, source_text="", url=URL, is_preprint=False, needs_url=True)
        == []
    )


def test_hook_rule_text_matches_what_is_enforced():
    assert str(HOOK_MAX_CHARS) in hook_rule(carries_url=False)
    assert "NO link" in hook_rule(carries_url=False)
    assert "NO link" not in hook_rule(carries_url=True)


# --- the conversation KPI --------------------------------------------------


def test_conversation_weighs_replies_quotes_and_bookmarks_over_likes():
    from feedback.models import KPIS, Metrics

    m = Metrics(impressions=1000, likes=10, reposts=2, replies=3, quotes=1, bookmarks=4)
    # 10*1 + 2*2 + 3*3 + 1*3 + 4*2
    assert m.conversation == 34
    assert m.get("conversation") == 34
    assert "conversation" in KPIS
    # a post with reach and no conversation scores nothing
    assert Metrics(impressions=100_000).conversation == 0
    # likes are the cheapest signal: 10 likes are worth less than 4 replies
    assert Metrics(likes=10).conversation < Metrics(replies=4).conversation


def test_swarm_fitness_can_select_on_conversation():
    from swarm import store as swarm_store

    assert "conversation" in swarm_store.KPIS
    assert swarm_store._metrics([1000, 10, 2, 3, 1, 4])["conversation"] == 34
