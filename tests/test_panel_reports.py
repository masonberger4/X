"""The publishing and feedback pages, and the read-only adapters behind them.

Both pages are read-only views of another step's tables: they must render before those
tables exist, and must never offer a way to post or to apply a suggestion.
"""

import json
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from ops import store as ops_store
from panel.app import app


@pytest.fixture
def client(db_file):
    return TestClient(app, follow_redirects=False)


def _publish_tables(conn):
    conn.executescript(
        """CREATE TABLE IF NOT EXISTS schedule (
             id INTEGER PRIMARY KEY AUTOINCREMENT, draft_id INTEGER NOT NULL UNIQUE,
             scheduled_for TEXT, claimed_at TEXT, finished_at TEXT, status TEXT, error TEXT);
           CREATE TABLE IF NOT EXISTS posts (
             id INTEGER PRIMARY KEY AUTOINCREMENT, draft_id INTEGER, tweet_id TEXT, text TEXT,
             kind TEXT, position INTEGER, posted_at TEXT, slot TEXT, status TEXT, error TEXT);"""
    )


def _feedback_tables(conn):
    conn.executescript(
        """CREATE TABLE IF NOT EXISTS tweet_metrics (
             id INTEGER PRIMARY KEY AUTOINCREMENT, tweet_id TEXT, draft_id INTEGER,
             captured_on TEXT, captured_at TEXT, impressions INTEGER DEFAULT 0,
             likes INTEGER DEFAULT 0, reposts INTEGER DEFAULT 0, replies INTEGER DEFAULT 0,
             quotes INTEGER DEFAULT 0, bookmarks INTEGER DEFAULT 0, deleted INTEGER DEFAULT 0);
           CREATE TABLE IF NOT EXISTS follower_snapshots (
             id INTEGER PRIMARY KEY AUTOINCREMENT, captured_on TEXT UNIQUE, captured_at TEXT,
             followers INTEGER, following INTEGER, tweet_count INTEGER);
           CREATE TABLE IF NOT EXISTS feedback_reports (
             id INTEGER PRIMARY KEY AUTOINCREMENT, window_start TEXT, window_end TEXT,
             generated_at TEXT, report_md TEXT, suggestions_json TEXT DEFAULT '[]');"""
    )


# ---- adapters ---------------------------------------------------------------


def test_the_adapters_are_empty_when_the_tables_are_missing(conn):
    assert ops_store.fetch_publish_rows(conn) == {"posts": [], "needs_attention": []}
    assert ops_store.fetch_follower_series(conn) == []
    assert ops_store.fetch_post_metrics(conn) == []
    assert ops_store.fetch_latest_feedback_report(conn) is None


def test_publish_rows_return_posts_newest_first_and_flag_what_needs_a_human(conn):
    _publish_tables(conn)
    conn.executemany(
        "INSERT INTO posts (draft_id, tweet_id, text, kind, position, posted_at, slot, status,"
        " error) VALUES (?,?,?,?,?,?,?,?,?)",
        [
            (1, "t1", "older", "single", 1, "2026-04-30T09:00:00+00:00", "am", "posted", None),
            (2, "t2", "newer", "single", 1, "2026-05-01T09:00:00+00:00", "am", "posted", None),
        ],
    )
    conn.executemany(
        "INSERT INTO schedule (draft_id, status, error) VALUES (?,?,?)",
        [(3, "partial", "thread died at 2/4"), (4, "posted", None)],
    )
    conn.commit()
    rows = ops_store.fetch_publish_rows(conn)
    assert [p["text"] for p in rows["posts"]] == ["newer", "older"]
    assert [r["draft_id"] for r in rows["needs_attention"]] == [3]
    assert rows["needs_attention"][0]["error"] == "thread died at 2/4"


def test_post_metrics_keep_only_the_latest_snapshot_per_tweet(conn):
    _feedback_tables(conn)
    conn.executemany(
        "INSERT INTO tweet_metrics (tweet_id, draft_id, captured_on, captured_at, impressions)"
        " VALUES (?,?,?,?,?)",
        [
            ("t1", 1, "2026-04-30", "2026-04-30T00:00:00+00:00", 100),
            ("t1", 1, "2026-05-01", "2026-05-01T00:00:00+00:00", 900),
            ("t2", 2, "2026-05-01", "2026-05-01T00:00:00+00:00", 400),
        ],
    )
    conn.commit()
    rows = ops_store.fetch_post_metrics(conn)
    assert [(r["tweet_id"], r["impressions"]) for r in rows] == [("t1", 900), ("t2", 400)]


def test_follower_series_is_oldest_first(conn):
    _feedback_tables(conn)
    conn.executemany(
        "INSERT INTO follower_snapshots (captured_on, captured_at, followers, following,"
        " tweet_count) VALUES (?,?,?,?,?)",
        [("2026-04-29", "x", 10, 5, 3), ("2026-04-30", "x", 14, 5, 4)],
    )
    conn.commit()
    series = ops_store.fetch_follower_series(conn)
    assert [s["followers"] for s in series] == [10, 14]


def test_latest_feedback_report_parses_its_suggestions(conn):
    _feedback_tables(conn)
    conn.execute(
        "INSERT INTO feedback_reports (window_start, window_end, generated_at, report_md,"
        " suggestions_json) VALUES (?,?,?,?,?)",
        (
            "2026-04-01",
            "2026-05-01",
            "2026-05-01T00:00:00+00:00",
            "# report",
            json.dumps([{"title": "raise the threshold", "detail": "low scorers underperform"}]),
        ),
    )
    conn.commit()
    report = ops_store.fetch_latest_feedback_report(conn)
    assert report["report_md"] == "# report"
    assert report["suggestions"][0]["title"] == "raise the threshold"


def test_a_corrupt_suggestions_blob_does_not_break_the_report(conn):
    _feedback_tables(conn)
    conn.execute(
        "INSERT INTO feedback_reports (window_start, window_end, generated_at, report_md,"
        " suggestions_json) VALUES ('a','b','2026-05-01T00:00:00+00:00','md','not json')"
    )
    conn.commit()
    assert ops_store.fetch_latest_feedback_report(conn)["suggestions"] == []


# ---- pages ------------------------------------------------------------------


def test_both_pages_render_before_their_tables_exist(client):
    body = client.get("/publishing").text
    assert "Publishing is off" not in body  # the shipped publish step is on (dry run)
    assert "approved, waiting" in body
    assert "No metrics yet" in client.get("/feedback").text


def test_publishing_shows_posts_and_what_needs_a_human(client, conn):
    _publish_tables(conn)
    conn.execute(
        "INSERT INTO posts (draft_id, tweet_id, text, kind, position, posted_at, slot, status)"
        " VALUES (7, 'tw7', 'ORR 88% in the phase 2', 'single', 1, ?, 'am', 'posted')",
        (datetime.now(UTC).isoformat(),),
    )
    conn.execute("INSERT INTO schedule (draft_id, status, error) VALUES (8, 'partial', 'died')")
    conn.commit()
    body = client.get("/publishing").text
    assert "ORR 88% in the phase 2" in body and "x.com/i/status/tw7" in body
    assert "Needs a human" in body and "died" in body


def test_publishing_never_offers_to_post(client):
    """The only form on the page edits the two caps; nothing here posts."""
    body = client.get("/publishing").text
    assert body.count("<form") == 1 and 'action="/publishing/caps"' in body
    assert "/publishing/now" not in body
    assert "--live" not in body


def test_feedback_charts_the_follower_trend_and_lists_the_proposals(client, conn):
    _feedback_tables(conn)
    conn.executemany(
        "INSERT INTO follower_snapshots (captured_on, captured_at, followers, following,"
        " tweet_count) VALUES (?,?,?,?,?)",
        [(f"2026-04-{d:02d}", "x", 100 + d, 5, d) for d in range(1, 8)],
    )
    conn.execute(
        "INSERT INTO feedback_reports (window_start, window_end, generated_at, report_md,"
        " suggestions_json) VALUES ('2026-04-01','2026-04-08','2026-04-08T00:00:00+00:00',"
        " '# full report', ?)",
        (json.dumps([{"title": "drop the 6pm slot", "detail": "it underperforms"}]),),
    )
    conn.commit()
    body = client.get("/feedback").text
    assert "<polyline" in body and "107" in body  # the trend, and where it ended
    assert "+6" in body  # growth over the window
    assert "drop the 6pm slot" in body and "PROPOSALS" in body
    assert "# full report" in body


def test_feedback_never_offers_to_apply_a_suggestion(client, conn):
    _feedback_tables(conn)
    conn.execute(
        "INSERT INTO feedback_reports (window_start, window_end, generated_at, report_md,"
        " suggestions_json) VALUES ('a','b','2026-05-01T00:00:00+00:00','md', ?)",
        (json.dumps([{"title": "raise threshold"}]),),
    )
    conn.commit()
    assert "<form" not in client.get("/feedback").text


def test_the_nav_reaches_every_page(client):
    body = client.get("/").text
    for link in ("/feed", "/publishing", "/feedback", "/sources", "/runs", "/queue"):
        assert f'href="{link}"' in body


def test_stale_snapshots_do_not_crash_the_growth_summary(client, conn):
    _feedback_tables(conn)
    conn.execute(
        "INSERT INTO follower_snapshots (captured_on, captured_at, followers, following,"
        " tweet_count) VALUES ('2026-01-01', 'x', 42, 5, 1)"
    )
    conn.commit()
    body = client.get("/feedback").text
    assert "42" in body and "<polyline" not in body  # one point is a number, not a line
