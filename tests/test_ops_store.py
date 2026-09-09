"""ops/store.py: own tables and read-only adapters against a temp SQLite file.

Uses conftest's db_file/conn fixtures (step 1 tables via db.Database, step 2 via
approval_queue.store.connect). Step 3/4 tables are created by hand where needed.
"""

import sqlite3
from datetime import UTC, datetime, timedelta

from db import Database
from draft.schema import Draft
from ops import store
from ops.runner import StepResult
from tests.conftest import seed_item

NOW = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)

SOURCES = [
    {"name": "pubmed", "cadence_minutes": 60, "enabled": True},
    {"name": "fda_press", "cadence_minutes": 60, "enabled": True},
    {"name": "never", "cadence_minutes": 60, "enabled": True},
    {"name": "off", "cadence_minutes": 60, "enabled": False},
]

PUBLISH_SCHEMA = """
CREATE TABLE IF NOT EXISTS schedule (
    id INTEGER PRIMARY KEY AUTOINCREMENT, draft_id INTEGER NOT NULL UNIQUE,
    scheduled_for TEXT, claimed_at TEXT, finished_at TEXT,
    status TEXT NOT NULL DEFAULT 'pending', error TEXT);
CREATE TABLE IF NOT EXISTS posts (
    id INTEGER PRIMARY KEY AUTOINCREMENT, draft_id INTEGER NOT NULL, tweet_id TEXT,
    text TEXT NOT NULL, kind TEXT NOT NULL, position INTEGER NOT NULL DEFAULT 1,
    posted_at TEXT, slot TEXT, status TEXT NOT NULL, error TEXT);
"""

FEEDBACK_SCHEMA = """
CREATE TABLE IF NOT EXISTS tweet_metrics (
    id INTEGER PRIMARY KEY, tweet_id TEXT, draft_id INTEGER, captured_on TEXT,
    captured_at TEXT, impressions INTEGER);
CREATE TABLE IF NOT EXISTS follower_snapshots (
    id INTEGER PRIMARY KEY, captured_on TEXT UNIQUE, captured_at TEXT, followers INTEGER);
CREATE TABLE IF NOT EXISTS feedback_reports (
    id INTEGER PRIMARY KEY, window_start TEXT, window_end TEXT, generated_at TEXT);
"""


def iso(dt: datetime) -> str:
    return dt.isoformat()


def result(name, exit_code=0, **kw) -> StepResult:
    return StepResult(name, ["python", f"run_{name}.py"], NOW, NOW, exit_code=exit_code, **kw)


# ---- adapters with no tables ----------------------------------------------------


def test_adapters_return_empty_on_bare_db(tmp_path):
    conn = store.connect(tmp_path / "empty.db")
    assert store.fetch_source_runs(conn, SOURCES) == [
        store.SourceRun("pubmed", True, 60),
        store.SourceRun("fda_press", True, 60),
        store.SourceRun("never", True, 60),
        store.SourceRun("off", False, 60),
    ]
    assert store.fetch_stage_activity(conn).tables_present is False
    assert store.fetch_publish_state(conn).tables_present is False
    assert store.fetch_feedback_state(conn).tables_present is False
    assert set(store.table_counts(conn)) == set(store.OWN_TABLES)
    conn.close()


def test_own_tables_are_created_and_others_untouched(db_file):
    before = store.tables(sqlite3.connect(db_file))
    conn = store.connect(db_file)
    after = store.tables(conn)
    assert after - before == set(store.OWN_TABLES)
    conn.close()


# ---- source runs ----------------------------------------------------------------


def test_fetch_source_runs_joins_config_and_rows(db_file):
    d = Database(str(db_file))
    d.record_run("pubmed", 10, 2, at=NOW - timedelta(minutes=10))
    d.record_run("fda_press", 0, 0, error="HTTP 503", at=NOW - timedelta(minutes=5))
    d.record_run("removed", 1, 1, at=NOW)
    d.close()
    conn = store.connect(db_file)
    runs = {r.source: r for r in store.fetch_source_runs(conn, SOURCES)}
    assert runs["pubmed"].last_run_at == NOW - timedelta(minutes=10)
    assert runs["pubmed"].fetched == 10 and runs["pubmed"].inserted == 2
    assert runs["pubmed"].error is None
    assert runs["fda_press"].error == "HTTP 503"
    assert runs["never"].last_run_at is None and runs["never"].enabled
    assert runs["off"].enabled is False
    assert runs["removed"].enabled is False  # in table, not in config
    conn.close()


# ---- stage activity -------------------------------------------------------------


def test_fetch_stage_activity(conn):
    # conn is step 2's connection on db_file; seed 2 scored items + 1 unscored cluster.
    cid = seed_item(conn, "a", hours_ago=1)
    seed_item(conn, "b", hours_ago=30)  # score older than 24h
    conn.execute(
        "INSERT INTO clusters (title, norm_title, created_at, prefilter_status)"
        " VALUES ('t', 't', ?, 'pass')",
        (iso(datetime.now(UTC)),),
    )
    conn.execute(
        "INSERT INTO clusters (title, norm_title, created_at, prefilter_status)"
        " VALUES ('d', 'd', ?, 'drop')",
        (iso(datetime.now(UTC)),),
    )
    from approval_queue import store as qstore

    draft = Draft(
        single_post="p https://doi.org/10.1000/x",
        thread=["a", "b", "c"],
        suggested_visual="",
        why_it_matters="",
    )
    did = qstore.insert_draft(conn, item_id="a", cluster_id=cid, model="m", draft=draft)
    did2 = qstore.insert_draft(conn, item_id="b", cluster_id=cid, model="m", draft=draft)
    qstore.approve(conn, did2)
    conn.commit()

    oconn = store.connect(conn.execute("PRAGMA database_list").fetchone()[2])
    act = store.fetch_stage_activity(oconn)
    assert act.tables_present
    assert act.latest_fetched_at is not None
    assert act.latest_scored_at is not None
    assert act.latest_draft_at is not None
    assert act.latest_decision_at is not None
    assert act.unscored_backlog == 1  # the 'pass' cluster without a score; 'drop' ignored
    assert act.pending_drafts == 1
    assert act.oldest_pending_age_hours is not None and act.oldest_pending_age_hours < 1
    assert act.approved_drafts == 1
    assert act.scores_24h == 1
    assert act.drafts_24h == 2
    assert did
    oconn.close()


# ---- publish / feedback ---------------------------------------------------------


def test_fetch_publish_state(tmp_path):
    conn = store.connect(tmp_path / "p.db")
    conn.executescript(PUBLISH_SCHEMA)
    rows = [
        (1, "posted", iso(NOW - timedelta(hours=2))),
        (2, "posted", iso(NOW - timedelta(days=3))),
        (3, "posted", iso(NOW - timedelta(days=10))),
        (4, "partial", iso(NOW - timedelta(days=1))),
        (5, "failed", iso(NOW - timedelta(days=9))),  # too old to count
        (6, "failed", iso(NOW - timedelta(hours=3))),
    ]
    for draft_id, status, when in rows:
        conn.execute(
            "INSERT INTO schedule (draft_id, status, claimed_at, finished_at) VALUES (?,?,?,?)",
            (draft_id, status, when, when),
        )
    conn.execute(
        "INSERT INTO schedule (draft_id, status, claimed_at) VALUES (7, 'claimed', ?)",
        (iso(NOW - timedelta(hours=2)),),
    )
    conn.execute(
        "INSERT INTO schedule (draft_id, status, claimed_at) VALUES (8, 'claimed', ?)",
        (iso(NOW - timedelta(minutes=5)),),
    )
    for draft_id, status, when in rows[:3]:
        conn.execute(
            "INSERT INTO posts (draft_id, text, kind, posted_at, status)"
            " VALUES (?, 'SECRET POST TEXT', 'single', ?, ?)",
            (draft_id, when, status),
        )
    conn.execute(
        "INSERT INTO posts (draft_id, text, kind, posted_at, status)"
        " VALUES (6, 'x', 'single', ?, 'failed')",
        (iso(NOW - timedelta(hours=3)),),
    )
    conn.commit()
    st = store.fetch_publish_state(conn, NOW)
    assert st.tables_present
    assert st.posts_24h == 1
    assert st.posts_7d == 2
    assert st.partial_7d == 1
    assert st.failed_7d == 1
    assert st.stuck_claimed == 1
    assert st.latest_posted_at == NOW - timedelta(hours=2)
    assert "SECRET" not in repr(st)
    conn.close()


def test_fetch_feedback_state(tmp_path):
    conn = store.connect(tmp_path / "f.db")
    conn.executescript(FEEDBACK_SCHEMA)
    st = store.fetch_feedback_state(conn)
    assert st.tables_present and st.latest_captured_on is None
    conn.execute(
        "INSERT INTO tweet_metrics (tweet_id, draft_id, captured_on, captured_at, impressions)"
        " VALUES ('1', 1, '2026-05-30', ?, 5)",
        (iso(NOW),),
    )
    conn.execute(
        "INSERT INTO follower_snapshots (captured_on, captured_at, followers)"
        " VALUES ('2026-05-31', ?, 12)",
        (iso(NOW),),
    )
    conn.execute(
        "INSERT INTO feedback_reports (window_start, window_end, generated_at)"
        " VALUES ('2026-05-24', '2026-05-31', ?)",
        (iso(NOW),),
    )
    conn.commit()
    st = store.fetch_feedback_state(conn)
    assert st.latest_captured_on == "2026-05-30"
    assert st.latest_snapshot_on == "2026-05-31"
    assert st.latest_report_at == NOW
    conn.close()


# ---- own tables -----------------------------------------------------------------


def test_record_and_last_run_per_step(tmp_path):
    conn = store.connect(tmp_path / "o.db")
    store.record_results(conn, "run1", [result("ingest"), result("score", 1)])
    store.record_results(
        conn,
        "run2",
        [result("ingest", None, timed_out=True), result("score", None, skipped_reason="x")],
    )
    last = store.last_run_per_step(conn)
    assert set(last) == {"ingest", "score"}
    assert last["ingest"]["run_id"] == "run2" and last["ingest"]["timed_out"] is True
    assert last["score"]["skipped_reason"] == "x"
    assert last["ingest"]["argv"] == ["python", "run_ingest.py"]
    assert last["ingest"]["started_at"] == NOW
    conn.close()


def test_health_and_alert_records_and_prune(tmp_path):
    from ops.models import Check, Report

    conn = store.connect(tmp_path / "o.db")
    assert store.last_health(conn) is None
    old = Report.build(NOW - timedelta(days=40), [Check("a", "fail", "bad")])
    new = Report.build(NOW, [Check("a", "ok", "fine")])
    store.record_health(conn, old)
    store.record_health(conn, new)
    lh = store.last_health(conn)
    assert lh["overall"] == "ok" and lh["checked_at"] == NOW
    assert lh["report"]["checks"][0]["summary"] == "fine"

    store.record_alert(conn, "a", "fail", "webhook", NOW - timedelta(days=40))
    store.record_alert(conn, "a", "warn", "webhook", NOW - timedelta(hours=1))
    store.record_alert(conn, "b", "fail", "log", NOW - timedelta(days=40))
    prior = store.last_alert_per_check(conn)
    assert prior["a"] == ("warn", NOW - timedelta(hours=1))
    assert prior["b"][0] == "fail"

    store.record_results(conn, "old", [StepResult("x", [], NOW - timedelta(days=40), NOW)])
    store.record_results(conn, "new", [result("x")])
    deleted = store.prune_own(conn, 30, now=NOW)
    assert deleted == {"pipeline_runs": 1, "health_checks": 1, "alerts_sent": 2}
    assert store.last_health(conn)["overall"] == "ok"
    assert set(store.last_alert_per_check(conn)) == {"a"}
    conn.close()


def test_prune_never_touches_other_tables(db_file):
    d = Database(str(db_file))
    d.record_run("pubmed", 1, 1, at=NOW - timedelta(days=400))
    d.close()
    conn = store.connect(db_file)
    store.prune_own(conn, 1, now=NOW)
    assert conn.execute("SELECT COUNT(*) FROM source_runs").fetchone()[0] == 1
    conn.close()


def test_db_path_resolution(monkeypatch, tmp_path):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "x.db"))
    assert store.db_path() == tmp_path / "x.db"
    monkeypatch.delenv("DB_PATH")
    assert store.db_path().name == "pipeline.db"  # from config.yaml's db_path
