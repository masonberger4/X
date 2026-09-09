"""Suggestions are conservative: nothing below min_posts, one for a clear-cut case."""

from datetime import UTC, datetime, timedelta

import pytest

from feedback import analysis as an
from feedback import suggest as sg
from feedback.models import Metrics

T0 = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)


def row(i, impressions, **kw):
    base = dict(
        draft_id=i,
        tweet_id=str(i),
        posted_at=T0 + timedelta(minutes=i),
        kind="single",
        slot="08:30",
        hour=8,
        source="pubmed",
        evidence_level="phase2",
        edited=False,
        topics=[],
        scores={},
        rating=None,
        title=f"t{i}",
        url="u",
        suggested_angle="",
        head=Metrics(impressions=impressions),
    )
    base.update(kw)
    return an.PostRow(**base)


def analysis(rows, kpi="impressions"):
    top, bottom = an.rank_rows(rows, kpi)
    return an.Analysis(
        window_start=T0 - timedelta(days=7),
        window_end=T0,
        kpi=kpi,
        rows=rows,
        groups={d: an.group_summaries(rows, d, kpi) for d in an.GROUP_DIMENSIONS},
        correlations=an.correlations(rows, kpi),
        followers=an.follower_delta([]),
        top=top,
        bottom=bottom,
    )


def test_no_suggestion_below_min_posts():
    # A huge effect, but only 3 posts per side.
    rows = [row(i, 1000, source="fda") for i in range(3)] + [
        row(i, 10, source="pubmed") for i in range(3, 6)
    ]
    assert sg.suggest(analysis(rows), min_posts=5) == []
    # Enough posts overall, but the interesting group is still too small.
    rows = [row(i, 1000, source="fda") for i in range(2)] + [
        row(i, 10, source="pubmed") for i in range(2, 12)
    ]
    assert sg.suggest(analysis(rows), min_posts=5) == []


def test_clear_cut_source_effect_produces_one_cadence_suggestion():
    rows = [row(i, 1000 + i, source="fda_press") for i in range(5)] + [
        row(i, 100 + i, source="pubmed") for i in range(5, 10)
    ]
    out = sg.suggest(analysis(rows), min_posts=5, effect_ratio=2.0)
    cadence = [s for s in out if s.kind == sg.KIND_CADENCE]
    assert len(cadence) == 2  # fda_press above, pubmed below: each names its own key
    above = next(s for s in cadence if "fda_press" in s.target)
    assert above.target == "config.yaml: sources[fda_press].cadence_minutes"
    assert "shorter cadence_minutes" in above.rationale
    assert "n=5, median impressions=1002" in above.evidence
    assert all(s.kind in sg.KINDS for s in out)


def test_small_effect_is_not_reported():
    rows = [row(i, 150, source="fda") for i in range(5)] + [
        row(i, 100, source="pubmed") for i in range(5, 10)
    ]
    assert sg.suggest(analysis(rows), min_posts=5, effect_ratio=2.0) == []


def test_rubric_weight_from_strong_correlation_only():
    rows = [row(i, 100 * (i + 1), scores={"novelty": i, "timeliness": 5}) for i in range(6)]
    out = sg.suggest(analysis(rows), min_posts=5, min_abs_rho=0.5)
    rubric = [s for s in out if s.kind == sg.KIND_RUBRIC]
    assert len(rubric) == 1
    assert rubric[0].target == "score/rubric.py: compute_total weight for novelty"
    assert "PROMPT_VERSION" in rubric[0].rationale
    assert rubric[0].evidence == "Spearman rho=+1.00 over n=6 posts"


def test_hype_risk_positive_correlation_keeps_penalty():
    rows = [row(i, 100 * (i + 1), scores={"hype_risk": i}) for i in range(6)]
    out = sg.suggest(analysis(rows), min_posts=5)
    assert len(out) == 1 and "do not remove it" in out[0].rationale


def test_format_slot_topic_and_evidence_suggestions():
    rows = [
        row(
            i,
            1000,
            kind="thread",
            slot="12:15",
            edited=True,
            evidence_level="approval",
            topics=["approval"],
        )
        for i in range(5)
    ] + [
        row(i, 100, kind="single", slot="08:30", evidence_level="preprint", topics=["preprint"])
        for i in range(5, 10)
    ]
    out = sg.suggest(analysis(rows), min_posts=5)
    kinds = {s.kind for s in out}
    assert kinds == {sg.KIND_FORMAT, sg.KIND_SLOT, sg.KIND_PREFILTER, sg.KIND_RUBRIC}
    targets = {s.target for s in out}
    assert "publish/config.yaml: post_format" in targets
    assert "draft/voice.md" in targets
    assert "publish/config.yaml: slots" in targets
    assert "config.yaml: prefilter.allow_keywords (approval)" in targets
    assert "score/rubric.py: compute_total / evidence_level=preprint" in targets
    fmt = next(s for s in out if s.target == "publish/config.yaml: post_format")
    assert "post_format: thread" in fmt.rationale


def test_off_slot_group_targets_breaking_rules():
    rows = [row(i, 1000, slot="off-slot") for i in range(5)] + [
        row(i, 100, slot="08:30") for i in range(5, 10)
    ]
    out = [s for s in sg.suggest(analysis(rows), min_posts=5) if s.kind == sg.KIND_SLOT]
    assert {s.target for s in out} == {
        "publish/config.yaml: breaking",
        "publish/config.yaml: slots",
    }


def test_unknown_kind_rejected():
    with pytest.raises(ValueError):
        sg.Suggestion("magic", "x", "y", "z")
