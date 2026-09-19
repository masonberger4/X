"""Rule 2 (no post carries a link) and rule 12 (the one-claim opener), draft/hook.py."""

from __future__ import annotations

import pytest

from draft.drafter import check_hard_rules
from draft.hook import HOOK_MAX_CHARS, hook_problems, hook_rule, link_problems
from draft.schema import Draft
from swarm.cells import cell_problems
from swarm.genome import CLOSER, HOOK

URL = "https://example.org/study"


def test_clean_hook_passes():
    assert hook_problems("The armoring worked in blood. It never showed up in tumor.") == []


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
    assert any("first post contains" in p for p in hook_problems(text))


def test_any_link_in_any_post_fails():
    assert any("contains a link" in p for p in link_problems(f"The armoring worked. {URL}"))
    assert any("contains a link" in p for p in link_problems("Worth reading: nejm.org/doi/x"))
    assert link_problems("The armoring worked in blood, not in tumor.") == []
    # a ratio or a dose is not a domain
    assert link_problems("2.5/3.0 mg/kg in 12 patients") == []


def test_opener_longer_than_the_cap_fails():
    long = "The armoring worked in blood but not in tumor. " * 8
    assert any("not a summary" in p for p in hook_problems(long))
    assert len(long) > HOOK_MAX_CHARS


def test_a_single_post_is_not_hook_capped():
    long = f"The armoring worked in blood but not in tumor. {'Detail. ' * 30}"
    assert hook_problems(long, max_chars=None) == []


def test_check_hard_rules_applies_the_hook_rule_and_the_link_ban():
    draft = Draft(
        thread=["1/3 Armoring worked in blood.", "The mechanism.", f"Takeaway. {URL}"],
        why_it_matters="",
        suggested_visual="",
    )
    problems = check_hard_rules(draft, url=URL, source="pubmed")
    assert any("position marker" in p for p in problems)
    assert any("thread[2] contains a link" in p for p in problems)


def test_check_hard_rules_passes_a_link_free_single_post():
    draft = Draft(
        thread=["Armoring worked in blood, not in tumor."],
        why_it_matters="",
        suggested_visual="",
    )
    assert check_hard_rules(draft, url=URL, source="pubmed") == []


def test_hook_cell_is_checked_and_no_cell_carries_a_link():
    kwargs = dict(source_text="", is_preprint=False)
    assert any(
        "position marker" in p
        for p in cell_problems("1/6 Armoring worked in blood.", slot=HOOK, **kwargs)
    )
    assert cell_problems("Takeaway for the thesis.", slot=CLOSER, **kwargs) == []
    assert any(
        "contains a link" in p for p in cell_problems(f"Takeaway. {URL}", slot=CLOSER, **kwargs)
    )


def test_hook_rule_text_matches_what_is_enforced():
    assert str(HOOK_MAX_CHARS) in hook_rule()
    assert "1/6" in hook_rule()
    assert str(HOOK_MAX_CHARS) not in hook_rule(capped=False)


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
