"""The studio's learning loop, the pure part (studio/learn.py): scores, arms, the lean, the
evidence text and the playbook rewrite's checks. No DB, no network, no clock."""

from __future__ import annotations

import json
import math
import random
from datetime import UTC, datetime, timedelta

import pytest

from studio import learn as L

T0 = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
VOICES = (
    "## Voices\nThe app gives each piece one.\n\n"
    '### desk_note: The desk note\nDry and quick. "I didn\'t expect that."\n\n'
    '### sceptic: The sceptic\nReads the footnotes. "This deal makes no sense to me."'
)
SECTIONS = "\n\n".join(
    VOICES if h == "## Voices" else f"{h}\n- a line"
    for h in (
        "## Openings",
        "## Substance the editor values",
        "## Mistakes caught in fact-checks (do not repeat)",
        "## Format",
        "## Cards",
        "## Voices",
        "## Still to learn (the feedback loop fills these in)",
    )
)
PLAYBOOK = "# Playbook\n\n" + SECTIONS + "\n"


def piece(pid: int, value: float, *, days: float = 0, **kw) -> L.Measured:
    fields = dict(
        piece_id=pid,
        draft_id=100 + pid,
        posted_at=T0 + timedelta(days=days),
        value=value,
        metrics={},
        title=f"Piece {pid}",
        opening=f"Opening of piece {pid}.",
        angle="deal_decoder",
        shape="long_post",
        hook_style="hard_number",
        cards=2,
    )
    fields.update(kw)
    return L.Measured(**fields)


def heads(values, *, start_days: float = -10) -> list[L.Head]:
    return [
        L.Head(draft_id=i, posted_at=T0 + timedelta(days=start_days + i), value=v)
        for i, v in enumerate(values)
    ]


# ---- the KPI and the snapshot --------------------------------------------------------------


def test_the_kpi_is_the_conversation_weights_by_default():
    m = {"replies": 2, "quotes": 1, "bookmarks": 3, "reposts": 1, "likes": 10, "impressions": 400}
    # 2*3 + 1*3 + 3*2 + 1*2 + 10*1 + 400*0.05 = 6 + 3 + 6 + 2 + 10 + 20
    assert L.kpi_value(m) == 47
    assert L.kpi_value(m, "likes") == 10
    assert L.kpi_value({}, "replies") == 0
    assert "replies x3" in L.describe_kpi() and L.describe_kpi("likes") == "likes"


def test_the_snapshot_is_the_first_one_past_the_horizon():
    posted = T0
    snaps = [
        {"captured_at": T0 + timedelta(hours=20), "n": 1},
        {"captured_at": T0 + timedelta(hours=50), "n": 2},
        {"captured_at": T0 + timedelta(hours=80), "n": 3},
    ]
    assert L.pick_snapshot(snaps, posted, 48)["n"] == 2
    assert L.pick_snapshot(snaps[:1], posted, 48) is None  # too young to score
    assert L.pick_snapshot(snaps, posted, 0)["n"] == 3  # no horizon: the newest
    assert L.pick_snapshot([], posted, 48) is None


def test_numbers_the_editor_typed_in_win_whatever_their_age():
    snaps = [
        {"captured_at": T0 + timedelta(hours=60), "n": "x"},
        {"captured_at": T0 + timedelta(hours=10), "n": "typed", "manual": True},
    ]
    assert L.pick_snapshot(snaps, T0, 48)["n"] == "typed"


# ---- scoring against the account's median --------------------------------------------------


def test_a_piece_is_scored_against_the_median_of_the_posts_before_it():
    [p] = L.score(
        [piece(1, 30)], heads([10, 20, 40]), baseline_days=30, min_baseline_posts=3, smoothing=0
    )
    assert p.baseline == 20 and p.relative == pytest.approx(1.5)
    assert p.log_rel == pytest.approx(math.log(1.5))


def test_smoothing_keeps_a_quiet_account_scoreable():
    [p] = L.score(
        [piece(1, 2)], heads([0, 0, 0]), baseline_days=30, min_baseline_posts=3, smoothing=1
    )
    assert p.baseline == 0 and p.relative == pytest.approx(3.0)
    [q] = L.score(
        [piece(2, 0)], heads([0, 0, 0]), baseline_days=30, min_baseline_posts=3, smoothing=0
    )
    assert q.relative is None  # zero over zero is no score


def test_too_few_posts_before_a_piece_leave_it_unscored():
    [p] = L.score(
        [piece(1, 30)], heads([10, 20]), baseline_days=30, min_baseline_posts=3, smoothing=1
    )
    assert (p.baseline, p.relative, p.log_rel) == (None, None, None)


def test_the_baseline_window_and_the_pieces_own_post_are_respected():
    old = [L.Head(draft_id=900, posted_at=T0 - timedelta(days=60), value=1000)]
    own = [L.Head(draft_id=101, posted_at=T0 - timedelta(hours=1), value=1000)]
    later = [L.Head(draft_id=901, posted_at=T0 + timedelta(days=1), value=1000)]
    [p] = L.score(
        [piece(1, 30)],
        heads([10, 20, 30]) + old + own + later,
        baseline_days=30,
        min_baseline_posts=3,
        smoothing=0,
    )
    assert p.baseline == 20  # the 60-day-old post, its own post and a later one are left out


def test_a_relative_of_zero_has_a_floor_for_its_logarithm():
    p = piece(1, 0)
    p.relative = 0.0
    assert p.log_rel == pytest.approx(math.log(L.LOG_FLOOR))


# ---- arms ----------------------------------------------------------------------------------


def scored_pieces() -> list[L.Measured]:
    rels = [
        (1, "catalyst_map", "thread", "question", 4, 2.0),
        (2, "catalyst_map", "long_post", "hard_number", 1, 3.0),
        (3, "deal_decoder", "long_post", "hard_number", 2, 1.0),
        (4, "one_chart", "short_post", "bold_claim", 0, 0.5),
        (5, "deal_decoder", "long_post", "question", 2, 1.0),
    ]
    out = []
    for pid, angle, shape, hook, cards, rel in rels:
        p = piece(pid, rel * 10, angle=angle, shape=shape, hook_style=hook, cards=cards)
        p.baseline, p.relative = 10.0, rel
        out.append(p)
    return out


def test_arm_stats_group_by_value_best_first():
    stats = L.arm_stats(scored_pieces(), "angle")
    assert [(s.value, s.n) for s in stats] == [
        ("catalyst_map", 2),
        ("deal_decoder", 2),
        ("one_chart", 1),
    ]
    assert stats[0].times == pytest.approx(math.sqrt(2.0 * 3.0))  # geometric mean
    assert stats[1].times == pytest.approx(1.0)


def test_cards_are_bucketed_and_unscored_pieces_left_out():
    pieces = scored_pieces() + [piece(9, 99, cards=7)]  # unscored: no baseline
    stats = {s.value: s.n for s in L.arm_stats(pieces, "cards")}
    assert stats == {"3+": 1, "1": 1, "2": 2, "0": 1}
    assert L.card_bucket(7) == "3+" and L.card_bucket(-1) == "0"


def test_the_posterior_starts_at_the_median_and_moves_with_evidence():
    assert L.posterior(None, prior_sd=0.5, post_sd=0.8) == (0.0, 0.5)
    one = L.ArmStat("angle", "x", 1, math.log(2))
    many = L.ArmStat("angle", "x", 20, math.log(2))
    m1, s1 = L.posterior(one, prior_sd=0.5, post_sd=0.8)
    m20, s20 = L.posterior(many, prior_sd=0.5, post_sd=0.8)
    assert 0 < m1 < m20 < math.log(2) and s20 < s1 < 0.5


def test_thompson_favours_what_did_better_and_still_tries_the_untried():
    stats = [
        L.ArmStat("angle", "strong", 8, math.log(2.5)),
        L.ArmStat("angle", "weak", 8, math.log(0.4)),
    ]
    rng = random.Random(7)
    picks = [
        L.thompson(["strong", "weak", "new"], stats, rng=rng, prior_sd=0.5, post_sd=0.8)
        for _ in range(2000)
    ]
    assert picks.count("strong") > picks.count("new") > picks.count("weak")
    assert picks.count("new") > 50  # little evidence is still explored
    with pytest.raises(ValueError):
        L.thompson([], stats, rng=rng, prior_sd=0.5, post_sd=0.8)


def test_the_spread_is_measured_only_once_there_is_enough_data():
    few = scored_pieces()
    assert L.spread(few, default=0.8, floor=0.2) == 0.8
    many = []
    for i in range(12):
        p = piece(i, 1)
        p.baseline, p.relative = 1.0, 1.0  # identical: no spread at all
        many.append(p)
    assert L.spread(many, default=0.8, floor=0.2) == 0.2  # floored


# ---- the lean ------------------------------------------------------------------------------


def test_no_lean_before_enough_pieces_are_measured():
    kw = dict(
        shapes=["long_post"],
        hooks=["question"],
        rng=random.Random(1),
        prior_sd=0.5,
        default_sd=0.8,
    )
    assert L.lean(scored_pieces()[:2], angles=["x", "y"], min_measured=3, **kw) is None
    assert L.lean([], angles=["x", "y"], min_measured=1, **kw) is None
    assert (
        L.lean(
            scored_pieces(),
            angles=["x", "y"],
            min_measured=3,
            hooks=[],
            **{k: v for k, v in kw.items() if k != "hooks"},
        )
        is None
    )


def test_an_angle_the_editor_chose_is_left_out_of_the_lean():
    got = L.lean(
        scored_pieces(),
        angles=["catalyst_map"],
        shapes=["long_post", "thread"],
        hooks=["question"],
        rng=random.Random(3),
        prior_sd=0.5,
        default_sd=0.8,
        min_measured=3,
    )
    assert got is not None and got.angle == ""
    assert got.line().startswith("If the story supports it, lean towards the shape ")
    assert "the angle" not in got.line() and " and the hook style question." in got.line()
    assert got.as_dict() == {"angle": "", "shape": got.shape, "hook_style": "question"}


def test_the_lean_only_picks_what_is_on_offer():
    offered = ["deal_decoder", "one_chart", "the_race"]  # catalyst_map is held back
    for seed in range(40):
        got = L.lean(
            scored_pieces(),
            angles=offered,
            shapes=["long_post", "thread"],
            hooks=["question", "story"],
            rng=random.Random(seed),
            prior_sd=0.5,
            default_sd=0.8,
            min_measured=3,
        )
        assert got is not None
        assert got.angle in offered and got.shape in ("long_post", "thread")
        assert got.hook_style in ("question", "story") and got.measured == 5
    line = got.line()
    assert line.startswith("If the story supports it, lean towards the angle ")
    assert "5 measured pieces" in line and "The story comes first" in line


def test_lean_shares_say_how_often_each_value_is_suggested():
    shares = L.lean_shares(
        scored_pieces(),
        "angle",
        ["catalyst_map", "deal_decoder", "one_chart", "never_tried"],
        rng=random.Random(7),
        prior_sd=0.5,
        default_sd=0.8,
        draws=4000,
    )
    assert abs(sum(shares.values()) - 1) < 1e-9
    assert max(shares, key=shares.get) == "catalyst_map"  # 2.4x on 2 pieces
    assert shares["never_tried"] > 0.05  # untried values keep a real chance
    assert shares["one_chart"] < shares["deal_decoder"]
    assert (
        L.lean_shares(
            scored_pieces(), "angle", [], rng=random.Random(1), prior_sd=0.5, default_sd=0.8
        )
        == {}
    )


# ---- the evidence block --------------------------------------------------------------------


def test_nothing_scored_means_no_evidence_block():
    assert L.evidence_block([]) == ""
    assert L.evidence_block([piece(1, 10)]) == ""  # measured but not scored


def test_the_evidence_block_reads_like_evidence_with_its_sample_size():
    text = L.evidence_block(scored_pieces(), horizon_hours=48, baseline_days=30)
    first = text.splitlines()[0]
    assert first.startswith("5 studio pieces measured on X so far")
    assert "48 hours after posting" in first and "30 days before it" in first
    assert "too few to be sure of anything" in text  # under SMALL_SAMPLE
    assert "- Angles: catalyst_map 2.4x (2 pieces); deal_decoder 1.0x (2 pieces); " in text
    assert "one_chart 0.5x (1 piece)" in text
    assert "- Cards per piece: " in text and "- Hook styles: " in text
    assert "- Did best: Piece 2 (catalyst_map, long_post, hard_number hook, 1 card): 3.0x" in text
    assert "- Did worst: Piece 4 (one_chart, short_post, bold_claim hook, 0 cards): 0.5x" in text
    assert 'It opened: "Opening of piece 2."' in text


def test_one_scored_piece_is_not_both_the_best_and_the_worst():
    p = piece(1, 20)
    p.baseline, p.relative = 10.0, 2.0
    text = L.evidence_block([p])
    assert text.count("Did best") == 1 and "Did worst" not in text


def test_a_long_arm_is_shown_as_its_top_and_its_bottom():
    pool = []
    for i in range(8):
        p = piece(i, 1, angle=f"angle_{i}")
        p.baseline, p.relative = 1.0, 1.0 + i / 10
        pool.append(p)
    line = next(ln for ln in L.evidence_block(pool).splitlines() if ln.startswith("- Angles"))
    assert line.count(";") == 5 and "; ... ; " in line  # three best, the gap, two worst


# ---- the playbook rewrite ------------------------------------------------------------------


def test_a_rewrite_is_due_after_enough_new_pieces_and_time():
    pool = scored_pieces()  # pieces 1-5, all scored
    now = T0 + timedelta(days=2)
    due = dict(now=now, min_hours=24)
    assert L.rewrite_due(pool, learned_from=(), last_rewrite_at=None, min_new=3, **due)
    assert not L.rewrite_due(pool, learned_from=(), last_rewrite_at=None, min_new=6, **due)
    earlier = now - timedelta(hours=30)
    # The last rewrite saw pieces 1-3: two are new, whatever their post dates.
    assert L.rewrite_due(pool, learned_from=[1, 2, 3], last_rewrite_at=earlier, min_new=2, **due)
    assert not L.rewrite_due(
        pool, learned_from=[1, 2, 3], last_rewrite_at=earlier, min_new=3, **due
    )
    assert [p.piece_id for p in L.unseen(pool, [1, 2, 3])] == [4, 5]
    too_soon = now - timedelta(hours=2)
    assert not L.rewrite_due(pool, learned_from=(), last_rewrite_at=too_soon, min_new=1, **due)
    unscored = [piece(9, 5)]  # measured, too few posts before it to score
    assert not L.rewrite_due(unscored, learned_from=(), last_rewrite_at=None, min_new=1, **due)


def test_the_rewrite_prompt_carries_the_evidence_the_pieces_and_the_edits():
    edits = [L.Edit(piece_id=3, before="Merck paid $400 million.", after="Merck paid $400M.")]
    system, user = L.rewrite_prompt(
        PLAYBOOK, scored_pieces(), edits, evidence="EVIDENCE TEXT", max_words=700
    )
    assert "at most 700 words" in system and "Never relax the lines" in system
    assert "## Mistakes caught in fact-checks (do not repeat)" in system
    assert user.startswith("THE CURRENT PLAYBOOK\n# Playbook")
    assert "WHAT X SAYS\nEVIDENCE TEXT" in user
    assert "- 2026-10-01 Piece 2: angle catalyst_map, long_post, hard_number hook" in user
    assert "3.00x the median (30 against 10)" in user
    assert 'BEFORE: "Merck paid $400 million."' in user and 'AFTER: "Merck paid $400M."' in user


def test_a_good_rewrite_is_read_with_its_changelog():
    reply = "Here it is:\n" + json.dumps(
        {"playbook": PLAYBOOK, "changelog": ["  Lead with  the number ", "", 7]}
    )
    got = L.parse_rewrite(reply, max_words=700)
    assert got.playbook == PLAYBOOK
    assert got.changelog == ["Lead with the number", "7"]


@pytest.mark.parametrize(
    ("reply", "why"),
    [
        ("I could not do it.", "not a JSON object"),
        ("{not json}", "not valid JSON"),
        (json.dumps({"changelog": []}), "has no playbook"),
        (json.dumps({"playbook": "   "}), "has no playbook"),
        (json.dumps({"playbook": PLAYBOOK.replace("## Cards", "## Pictures")}), "lacks ## Cards"),
        (json.dumps({"playbook": PLAYBOOK + "word " * 900}), "runs"),
    ],
)
def test_a_rewrite_that_cannot_be_used_is_rejected(reply, why):
    with pytest.raises(L.RewriteRejected, match=why):
        L.parse_rewrite(reply, max_words=700)


def test_the_seed_playbook_already_has_every_section_but_cards():
    from studio.settings import DEFAULT_PLAYBOOK

    seed = DEFAULT_PLAYBOOK.read_text(encoding="utf-8")
    missing = [s for s in L.REQUIRED_SECTIONS if s not in seed]
    assert missing == ["## Cards"]  # the first rewrite adds it
