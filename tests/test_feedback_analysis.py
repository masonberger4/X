"""Pure analysis tests with hand-built rows. No DB, no network."""

from datetime import UTC, datetime, timedelta

import pytest

from feedback import analysis as an
from feedback.models import (
    FollowerSnapshot,
    Metrics,
    PostContext,
    PostedTweet,
    TweetSnapshot,
)

T0 = datetime(2026, 6, 1, 12, 30, tzinfo=UTC)


def posted(
    post_id, draft_id, tweet_id, *, kind="single", position=1, slot="2026-06-01 08:30", hours=0
):
    return PostedTweet(
        post_id=post_id,
        draft_id=draft_id,
        tweet_id=tweet_id,
        kind=kind,
        position=position,
        posted_at=T0 + timedelta(hours=hours),
        slot=slot,
    )


def snap(tweet_id, draft_id, day=0, deleted=False, **m):
    when = T0 + timedelta(days=day)
    return TweetSnapshot(
        tweet_id=tweet_id,
        draft_id=draft_id,
        captured_on=when.strftime("%Y-%m-%d"),
        captured_at=when,
        metrics=Metrics(**m),
        deleted=deleted,
    )


def ctx(
    draft_id,
    *,
    source="pubmed",
    evidence="phase2",
    edited=False,
    title="A CAR-T trial",
    rating=None,
    **scores,
):
    return PostContext(
        draft_id=draft_id,
        item_id=f"i{draft_id}",
        cluster_id=draft_id,
        source=source,
        url=f"https://x/{draft_id}",
        title=title,
        evidence_level=evidence,
        scores=scores,
        rating=rating,
        suggested_angle=f"angle {draft_id}",
        edited=edited,
    )


def row(
    draft_id,
    impressions,
    *,
    source="pubmed",
    kind="single",
    slot="08:30",
    edited=False,
    evidence="phase2",
    topics=(),
    scores=None,
    rating=None,
    likes=0,
    hour=8,
):
    return an.PostRow(
        draft_id=draft_id,
        tweet_id=str(draft_id),
        posted_at=T0 + timedelta(minutes=draft_id),
        kind=kind,
        slot=slot,
        hour=hour,
        source=source,
        evidence_level=evidence,
        edited=edited,
        topics=list(topics),
        scores=scores or {},
        rating=rating,
        title=f"t{draft_id}",
        url="u",
        suggested_angle=f"angle {draft_id}",
        head=Metrics(impressions=impressions, likes=likes),
    )


# ---- Metrics ------------------------------------------------------------------


def test_engagement_rate_zero_impressions_is_zero_not_error():
    assert Metrics(impressions=0, likes=5).engagement_rate == 0.0
    assert Metrics(impressions=200, likes=5, reposts=3, replies=1, quotes=1).engagement_rate == 0.05


def test_metrics_add_and_get():
    s = Metrics(impressions=10, likes=1) + Metrics(impressions=5, bookmarks=2)
    assert (s.impressions, s.likes, s.bookmarks) == (15, 1, 2)
    with pytest.raises(KeyError):
        s.get("nope")


# ---- Snapshots and rows -----------------------------------------------------------


def test_latest_snapshot_per_tweet():
    latest = an.latest_snapshots(
        [
            snap("a", 1, day=0, impressions=10),
            snap("a", 1, day=2, impressions=30),
            snap("a", 1, day=1, impressions=20),
            snap("b", 2, impressions=5),
        ]
    )
    assert latest["a"].metrics.impressions == 30
    assert latest["b"].metrics.impressions == 5


def test_thread_aggregation_head_plus_replies_shown_separately():
    tweets = [
        posted(1, 7, "h", kind="thread", position=1),
        posted(2, 7, "r1", kind="thread", position=2),
        posted(3, 7, "r2", kind="thread", position=3),
        posted(4, 7, "r3", kind="thread", position=4),  # no snapshot yet
    ]
    latest = an.latest_snapshots(
        [
            snap("h", 7, impressions=1000, likes=10),
            snap("r1", 7, impressions=300, likes=2),
            snap("r2", 7, impressions=200, likes=1),
        ]
    )
    rows = an.build_rows(tweets, {7: ctx(7)}, latest, timezone="America/New_York")
    assert len(rows) == 1
    r = rows[0]
    assert r.kind == "thread" and r.tweet_id == "h"
    assert r.head.impressions == 1000 and r.kpi("impressions") == 1000
    assert r.reply_sum == Metrics(impressions=500, likes=3) and r.reply_count == 2
    assert r.total.impressions == 1500 and r.total.likes == 13
    assert r.hour == 8  # 12:30 UTC is 08:30 in New York on 2026-06-01
    assert r.slot == "08:30"


def test_rows_skip_deleted_or_unsnapshotted_heads_and_count_deleted_replies():
    tweets = [
        posted(1, 1, "a"),
        posted(2, 2, "b"),
        posted(3, 3, "c", kind="thread", position=1, slot=None),
        posted(4, 3, "c2", kind="thread", position=2, slot=None),
    ]
    latest = an.latest_snapshots(
        [snap("a", 1, deleted=True), snap("c", 3, impressions=9), snap("c2", 3, deleted=True)]
    )
    rows = an.build_rows(tweets, {}, latest)
    assert [r.draft_id for r in rows] == [3]
    assert rows[0].reply_deleted == 1 and rows[0].reply_count == 0
    assert rows[0].slot == "off-slot" and rows[0].source == "unknown"


def test_tag_topics_case_insensitive():
    topics = {"cell_therapy": ["CAR-T", "TIL"], "adc": ["antibody-drug conjugate"]}
    assert an.tag_topics("A phase 2 car-t study", topics) == ["cell_therapy"]
    assert an.tag_topics("Nothing here", topics) == []
    assert an.tag_topics("x", None) == []


# ---- Groups --------------------------------------------------------------------


def test_group_summaries_median_mean_and_pooled_engagement():
    rows = [
        row(1, 100, source="pubmed", likes=10),
        row(2, 300, source="pubmed", likes=0),
        row(3, 50, source="fda", likes=5),
    ]
    by_source = {s.group: s for s in an.group_summaries(rows, "source", "impressions")}
    assert by_source["pubmed"].n == 2
    assert by_source["pubmed"].median_kpi == 200 and by_source["pubmed"].mean_kpi == 200
    assert by_source["pubmed"].engagement_rate == pytest.approx(10 / 400)
    assert by_source["fda"].engagement_rate == pytest.approx(0.1)
    # ordered best median first
    assert [s.group for s in an.group_summaries(rows, "source", "impressions")] == ["pubmed", "fda"]


def test_group_dimensions_edited_topic_hour_and_untagged():
    rows = [row(1, 10, edited=True, topics=("adc", "cell_therapy"), hour=8), row(2, 20, hour=12)]
    assert set(an.group_rows(rows, "edited")) == {"edited", "unedited"}
    assert set(an.group_rows(rows, "topic")) == {"adc", "cell_therapy", "untagged"}
    assert set(an.group_rows(rows, "hour")) == {"08:00", "12:00"}
    with pytest.raises(KeyError):
        an.group_rows(rows, "colour")


def test_compare_group_in_vs_out():
    rows = [row(1, 100, source="a"), row(2, 200, source="a"), row(3, 10, source="b")]
    assert an.compare_group(rows, "source", "a", "impressions") == (2, 150.0, 1, 10.0)
    assert an.compare_group(rows, "source", "zzz", "impressions") == (0, None, 3, 100.0)


# ---- Spearman --------------------------------------------------------------------


def test_spearman_known_answers():
    assert an.spearman([1, 2, 3, 4, 5], [5, 6, 7, 8, 7]) == pytest.approx(0.8207826816681233)
    assert an.spearman([1, 2, 3], [3, 2, 1]) == pytest.approx(-1.0)
    assert an.spearman([1, 2, 3], [10, 20, 30]) == pytest.approx(1.0)


def test_spearman_with_ties_uses_average_ranks():
    # scipy.stats.spearmanr([1, 2, 2, 3], [1, 3, 2, 4]).correlation == 0.9486832980505138
    assert an.spearman([1, 2, 2, 3], [1, 3, 2, 4]) == pytest.approx(0.9486832980505138)
    # ranks x=[1.5,1.5,3.5,3.5,5], y=[2.5,1,2.5,4.5,4.5]: cov 7.25 over sqrt(9*9) = 29/36
    assert an.spearman([1, 1, 2, 2, 3], [2, 1, 2, 3, 3]) == pytest.approx(29 / 36)


def test_spearman_degenerate_inputs():
    assert an.spearman([1, 2], [1, 2]) is None  # too few
    assert an.spearman([1, 1, 1], [1, 2, 3]) is None  # no variance
    with pytest.raises(ValueError):
        an.spearman([1, 2, 3], [1, 2])


def test_correlations_skip_missing_values():
    rows = [
        row(1, 10, scores={"novelty": 1, "total": 5}, rating=1),
        row(2, 20, scores={"novelty": 2, "total": 6}, rating=2),
        row(3, 30, scores={"novelty": 3, "total": 7}),  # no rating
        row(4, 40, scores={"total": 8}, rating=4),  # no novelty
    ]
    by_name = {c.name: c for c in an.correlations(rows, "impressions")}
    assert by_name["novelty"].n == 3 and by_name["novelty"].rho == pytest.approx(1.0)
    assert by_name["total"].n == 4 and by_name["total"].rho == pytest.approx(1.0)
    assert by_name["rating"].n == 3 and by_name["rating"].rho == pytest.approx(1.0)
    assert by_name["hype_risk"].n == 0 and by_name["hype_risk"].rho is None


# ---- Followers, ranking, analyse ------------------------------------------------------


def fsnap(day, followers):
    when = T0 + timedelta(days=day)
    return FollowerSnapshot(when.strftime("%Y-%m-%d"), when, followers, 10, 100)


def test_follower_delta_single_snapshot_is_none_not_zero():
    assert an.follower_delta([]).delta is None
    single = an.follower_delta([fsnap(0, 120)])
    assert single.delta is None and single.end.followers == 120
    two = an.follower_delta([fsnap(3, 150), fsnap(0, 120)])
    assert two.delta == 30 and two.start.followers == 120


def test_rank_rows_top_and_bottom():
    rows = [row(i, i * 10) for i in range(1, 8)]
    top, bottom = an.rank_rows(rows, "impressions", 3)
    assert [r.draft_id for r in top] == [7, 6, 5]
    assert [r.draft_id for r in bottom] == [1, 2, 3]
    top, bottom = an.rank_rows(rows[:2], "impressions", 3)
    assert [r.draft_id for r in top] == [2, 1] and [r.draft_id for r in bottom] == [1, 2]


def test_analyse_end_to_end_pure():
    tweets = [posted(1, 1, "a"), posted(2, 2, "b", slot="2026-06-01 12:15", hours=4)]
    contexts = {
        1: ctx(1, novelty=8, total=40, rating=5),
        2: ctx(2, source="fda", novelty=3, total=20, rating=2, title="FDA approves"),
    }
    snaps = [snap("a", 1, impressions=500, likes=5), snap("b", 2, impressions=100)]
    a = an.analyse(
        tweets,
        contexts,
        snaps,
        [fsnap(0, 100), fsnap(6, 130)],
        window_start=T0 - timedelta(days=7),
        window_end=T0,
        topics={"approval": ["FDA"]},
    )
    assert a.n_posts == 2 and a.n_threads == 0
    assert a.head_total.impressions == 600 and a.median_kpi == 300
    assert a.followers.delta == 30
    assert [t.draft_id for t in a.top] == [1, 2]
    assert {s.group for s in a.groups["topic"]} == {"approval", "untagged"}
    assert {s.group for s in a.groups["slot"]} == {"08:30", "12:15"}
    assert set(a.groups) == set(an.GROUP_DIMENSIONS)
