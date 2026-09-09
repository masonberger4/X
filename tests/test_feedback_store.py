"""feedback/store.py: the schedule with a frozen clock, and the adapters on real schemas."""

from datetime import UTC, datetime, timedelta

import pytest

from approval_queue import store as qstore
from db import Database
from draft.schema import Draft
from feedback import store
from feedback.models import Metrics, TweetMetrics, UserMetrics
from tests.conftest import seed_item

POSTED = datetime(2026, 6, 1, 12, 30, tzinfo=UTC)
SCHED = store.Schedule(daily_for_days=3, weekly_interval_days=7, stop_after_days=30)

# The real step 3 table (publish/store.py) so the fixture stays independent of that package.
POSTS_DDL = """
CREATE TABLE IF NOT EXISTS posts (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    draft_id  INTEGER NOT NULL,
    tweet_id  TEXT,
    text      TEXT NOT NULL,
    kind      TEXT NOT NULL,
    position  INTEGER NOT NULL DEFAULT 1,
    posted_at TEXT,
    slot      TEXT,
    status    TEXT NOT NULL,
    error     TEXT
)
"""


def insert_post(
    conn,
    draft_id,
    tweet_id,
    *,
    kind="single",
    position=1,
    posted_at=POSTED,
    slot="2026-06-01 08:30",
    status="posted",
    error=None,
):
    conn.execute(POSTS_DDL)
    conn.execute(
        "INSERT INTO posts (draft_id, tweet_id, text, kind, position, posted_at, slot, status,"
        " error) VALUES (?, ?, 'txt', ?, ?, ?, ?, ?, ?)",
        (
            draft_id,
            tweet_id,
            kind,
            position,
            posted_at.isoformat() if posted_at else None,
            slot,
            status,
            error,
        ),
    )
    conn.commit()


@pytest.fixture
def fconn(db_file):
    """Temp DB with step 1 (db.py) + step 2 (approval_queue) + step 4 tables."""
    Database(str(db_file)).close()
    qstore.connect(db_file).close()
    c = store.connect(db_file)
    yield c
    c.close()


# ---- schedule (pure, frozen clock) -------------------------------------------------


def run_days(schedule, days, *, deleted=False):
    """Simulate a daily cron: returns the day offsets on which a fetch happens."""
    last = None
    fetched = []
    for d in days:
        now = POSTED + timedelta(days=d, hours=3)
        if store.snapshot_due(POSTED, now, last, deleted, schedule):
            fetched.append(d)
            last = store.day_of(now)
    return fetched


def test_schedule_daily_then_weekly_then_never():
    got = run_days(SCHED, range(0, 40))
    assert got == [0, 1, 2, 3, 10, 17, 24]


def test_schedule_second_run_same_day_fetches_nothing():
    today = store.day_of(POSTED)
    assert store.snapshot_due(POSTED, POSTED, None, False, SCHED)
    assert not store.snapshot_due(POSTED, POSTED + timedelta(hours=5), today, False, SCHED)


def test_schedule_deleted_never_and_future_never():
    assert run_days(SCHED, range(0, 40), deleted=True) == []
    assert not store.snapshot_due(POSTED, POSTED - timedelta(days=1), None, False, SCHED)


def test_schedule_catches_up_after_missed_days():
    # Cron was down between day 3 and day 15: the first run back fetches (weekly phase).
    assert run_days(SCHED, [0, 1, 2, 3, 15, 16, 22, 23]) == [0, 1, 2, 3, 15, 22]


def test_schedule_from_config_defaults():
    s = store.Schedule.from_config({"snapshot": {"daily_for_days": 2}})
    assert (s.daily_for_days, s.weekly_interval_days, s.stop_after_days) == (2, 7, 90)
    assert store.Schedule.from_config(None) == store.Schedule()


def test_day_of_uses_utc():
    assert store.day_of(datetime(2026, 6, 1, 23, 30, tzinfo=UTC)) == "2026-06-01"
    assert store.day_of(datetime(2026, 6, 1, 23, 30)) == "2026-06-01"  # naive = UTC


# ---- adapters on the real schemas -------------------------------------------------


def test_fetch_posted_filters_eligibility_and_since(fconn):
    insert_post(fconn, 1, "a")
    insert_post(fconn, 2, None, status="failed", error="HTTP 500")
    insert_post(fconn, 3, "c", status="posted", error="late error")
    insert_post(fconn, 4, "d", posted_at=POSTED - timedelta(days=5))
    insert_post(fconn, 5, "h", kind="thread", position=1)
    insert_post(fconn, 5, "r", kind="thread", position=2)
    got = store.fetch_posted(None, fconn)
    assert [(p.draft_id, p.tweet_id, p.position) for p in got] == [
        (4, "d", 1),
        (1, "a", 1),
        (5, "h", 1),
        (5, "r", 2),
    ]
    assert got[1].posted_at == POSTED and got[1].slot == "2026-06-01 08:30"
    recent = store.fetch_posted(POSTED - timedelta(days=1), fconn)
    assert [p.tweet_id for p in recent] == ["a", "h", "r"]


def test_fetch_posted_without_posts_table(db_file):
    c = store.connect(db_file)
    assert store.fetch_posted(None, c) == []
    c.close()


def seed_draft(conn, item_id, *, source="pubmed", edit_to=None, rating=None, total=40):
    cid = seed_item(conn, item_id, source=source, total=total)
    d = Draft(
        single_post=f"Post {item_id}", thread=["one", "two"], suggested_visual="", why_it_matters=""
    )
    did = qstore.insert_draft(conn, item_id=item_id, cluster_id=cid, model="m", draft=d)
    if edit_to:
        qstore.edit(conn, did, single_post=edit_to, thread=["one", "two"])
    else:
        qstore.approve(conn, did)
    if rating is not None:
        conn.execute(
            "INSERT INTO ratings (cluster_id, rating, note, rated_at) VALUES (?, ?, '', ?)",
            (cid, rating, POSTED.isoformat()),
        )
        conn.commit()
    return did, cid


def test_fetch_post_context_joins_steps_1_and_2(fconn):
    d1, c1 = seed_draft(fconn, "a", source="fda_press", rating=4)
    d2, _ = seed_draft(fconn, "b", edit_to="Edited b")
    # a later re-score for cluster 1 wins, and a later rating wins
    fconn.execute(
        """INSERT INTO scores (cluster_id, model, prompt_version, novelty, clinical_significance,
           audience_interest, expertise_fit, timeliness, evidence_level, hype_risk, total,
           rationale, suggested_angle, raw_response, scored_at)
           VALUES (?, 'm', 'v2', 1, 2, 3, 4, 5, 'approval', 6, 12, 'r', 'new angle', '{}', ?)""",
        (c1, POSTED.isoformat()),
    )
    fconn.execute(
        "INSERT INTO ratings (cluster_id, rating, note, rated_at) VALUES (?, 2, '', ?)",
        (c1, POSTED.isoformat()),
    )
    fconn.commit()
    ctx = store.fetch_post_context([d1, d2, 999], fconn)
    assert set(ctx) == {d1, d2}
    a = ctx[d1]
    assert a.source == "fda_press" and a.title == "Title a" and a.cluster_id == c1
    assert a.evidence_level == "approval" and a.suggested_angle == "new angle"
    assert a.scores == {
        "novelty": 1,
        "clinical_significance": 2,
        "audience_interest": 3,
        "expertise_fit": 4,
        "timeliness": 5,
        "hype_risk": 6,
        "total": 12,
    }
    assert a.rating == 2 and not a.edited
    b = ctx[d2]
    assert b.edited and b.rating is None and b.scores["total"] == 40
    assert store.fetch_post_context([], fconn) == {}


# ---- own tables and due_for_snapshot -------------------------------------------------


def test_record_and_fetch_snapshots_one_per_day(fconn):
    now = POSTED
    tm = TweetMetrics("a", Metrics(impressions=10, likes=1))
    store.record_tweet_metrics(fconn, 1, tm, now)
    store.record_tweet_metrics(fconn, 1, TweetMetrics("a", Metrics(impressions=15)), now)
    store.record_tweet_metrics(
        fconn, 1, TweetMetrics("a", Metrics(impressions=20)), now + timedelta(days=1)
    )
    store.record_tweet_metrics(fconn, 2, TweetMetrics("b", deleted=True), now)
    snaps = store.fetch_tweet_snapshots(fconn, ["a", "b"])
    assert [(s.tweet_id, s.captured_on, s.metrics.impressions, s.deleted) for s in snaps] == [
        ("a", "2026-06-01", 15, False),
        ("b", "2026-06-01", 0, True),
        ("a", "2026-06-02", 20, False),
    ]
    assert len(store.fetch_tweet_snapshots(fconn)) == 3
    assert store.fetch_tweet_snapshots(fconn, []) == []


def test_due_for_snapshot_end_to_end(fconn):
    insert_post(fconn, 1, "a")
    insert_post(fconn, 2, "b")
    insert_post(fconn, 3, "old", posted_at=POSTED - timedelta(days=100))
    due = store.due_for_snapshot(POSTED, fconn, SCHED)
    assert [p.tweet_id for p in due] == ["a", "b"]
    store.record_tweet_metrics(fconn, 1, TweetMetrics("a", Metrics(impressions=1)), POSTED)
    store.record_tweet_metrics(fconn, 2, TweetMetrics("b", deleted=True), POSTED)
    assert store.due_for_snapshot(POSTED + timedelta(hours=2), fconn, SCHED) == []
    # next day: only the live tweet; deleted stays out even with --all
    nxt = POSTED + timedelta(days=1)
    assert [p.tweet_id for p in store.due_for_snapshot(nxt, fconn, SCHED)] == ["a"]
    assert [
        p.tweet_id for p in store.due_for_snapshot(nxt, fconn, SCHED, ignore_schedule=True)
    ] == ["a"]
    # weekly phase: day 5 not due normally, but --all fetches it
    day5 = POSTED + timedelta(days=5)
    store.record_tweet_metrics(
        fconn, 1, TweetMetrics("a", Metrics(impressions=3)), POSTED + timedelta(days=3)
    )
    assert store.due_for_snapshot(day5, fconn, SCHED) == []
    assert [
        p.tweet_id for p in store.due_for_snapshot(day5, fconn, SCHED, ignore_schedule=True)
    ] == ["a"]


def test_follower_snapshots_and_reports(fconn):
    assert not store.has_follower_snapshot(fconn, POSTED)
    store.record_follower_snapshot(fconn, UserMetrics("u", 100, 10, 50), POSTED)
    store.record_follower_snapshot(fconn, UserMetrics("u", 101, 10, 50), POSTED)  # same day
    store.record_follower_snapshot(fconn, UserMetrics("u", 130, 11, 55), POSTED + timedelta(days=6))
    assert store.has_follower_snapshot(fconn, POSTED)
    allsnaps = store.fetch_follower_snapshots(fconn)
    assert [(s.captured_on, s.followers) for s in allsnaps] == [
        ("2026-06-01", 101),
        ("2026-06-07", 130),
    ]
    window = store.fetch_follower_snapshots(
        fconn, POSTED + timedelta(days=2), POSTED + timedelta(days=9)
    )
    assert [s.followers for s in window] == [130]
    rid = store.record_report(
        fconn,
        window_start=POSTED - timedelta(days=7),
        window_end=POSTED,
        report_md="# r",
        suggestions=[{"kind": "slot"}],
        now=POSTED,
    )
    rows = store.list_reports(fconn)
    assert rid == 1 and len(rows) == 1 and rows[0]["report_md"] == "# r"
    assert rows[0]["suggestions_json"] == '[{"kind": "slot"}]'


def test_db_path_resolution(monkeypatch, tmp_path):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "x.db"))
    assert store.db_path() == tmp_path / "x.db"
    monkeypatch.delenv("DB_PATH")
    monkeypatch.chdir(tmp_path)
    assert str(store.db_path()) in ("pipeline.db", "./pipeline.db")
