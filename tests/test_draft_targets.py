"""Where a post cites a price target (draft/targets.py): the phrases, the figures written
next to the word target, and the biotech senses of "target" that are not price targets."""

from __future__ import annotations

import pytest

from draft.targets import target_figures, target_mentions, target_problems

IOVANCE = (
    "The stock closed at $14.45, up 31.5%. H.C. Wainwright raised its target to $20 from $9, "
    "and Wells Fargo to $18 from $14 the next day; Goldman had put a Buy and a $15 target on "
    "it five days earlier. At Friday's close of $14.22 the company is worth about $6.4B, and "
    "the shares sit above the average analyst target (~$12.40 on MarketBeat's tally)."
)


def test_every_target_in_a_real_passage_and_no_share_price():
    assert target_figures(IOVANCE) == ["20", "9", "18", "14", "15", "12.40"]
    assert target_mentions(IOVANCE) == [
        "target to $20",
        "$15 target",
        "average analyst target (~$12.40",
    ]


@pytest.mark.parametrize(
    "text, figures",
    [
        ("Jefferies set a price target of $40.", ["40"]),
        ("Guggenheim cut its price target to $25 after the interim look.", ["25"]),
        ("Shares at $14 trade above the $12.40 consensus target.", ["12.40"]),
        ("Stifel's $38 target (cut from $45) values only NSCLC.", ["38", "45"]),
        ("Stifel's $38 target, and Guggenheim at $40, both leave out NSCLC.", ["38", "40"]),
        ("Stifel: Buy, PT $38 (from $45). Guggenheim $38 PT.", ["38", "45"]),
        ("Its $38 12-month price target assumes a 2028 launch.", ["38"]),
        ("The target price of ~$22 is H.C. Wainwright's.", ["22"]),
        ("Morningstar's fair value estimate of $60 assumes a 2028 launch.", ["60"]),
        ("That puts fair value near $30.", ["30"]),
        ("The consensus price target sits at $12.40.", ["12.40"]),
    ],
)
def test_the_figures_a_text_gives_as_targets(text, figures):
    assert target_figures(text) == figures
    assert target_mentions(text)


@pytest.mark.parametrize(
    "text",
    [
        "Ivonescimab targets PD-1 and VEGF.",
        "The target population is 780 patients, with target enrollment of 300.",
        "It is chasing a $5B target market.",
        "A 2027 revenue target of $500M, or $2 billion by 2030.",
        "Revenue guidance went to $410-420M; the target is profitability.",
        "On-target toxicity was low; the targeted therapy lists at $450K.",
        "The deal's fair value of $45M was booked as goodwill.",
        "Pts on the PT arm.",  # "PT" counts only with a dollar figure
        "The stock closed at $14.45 after the readout.",
    ],
)
def test_biotech_targets_and_other_dollars_are_not_price_targets(text):
    assert target_mentions(text) == []
    assert target_figures(text) == []
    assert target_problems(text) == []


def test_a_mention_inside_a_longer_one_is_named_once():
    assert target_mentions("Jefferies set a price target of $40.") == ["price target of $40"]


def test_the_drafter_cites_no_target():
    [problem] = target_problems("H.C. Wainwright raised its target to $20 from $9.")
    assert problem.startswith("cites a price target ('target to $20')")
    assert "a thread cannot say what a target rests on" in problem
