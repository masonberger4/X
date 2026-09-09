"""Ops-owned tables plus read-only adapters onto every other step's tables.

Owns three tables (CREATE TABLE IF NOT EXISTS in the shared pipeline DB):

  pipeline_runs(id PK, run_id, step, argv, started_at, finished_at, exit_code,
                timed_out, skipped_reason, stdout_tail, stderr_tail)
  health_checks(id PK, checked_at, overall, report_json)
  alerts_sent(id PK, check_name, status, sent_at, channel)

Every other table is read-only here. Each adapter documents the schema it assumes and
returns an empty result (never raises) when the table does not exist yet, so a step
that has not merged simply shows up as 'skip' in health reports. prune_own() deletes
only from the three tables above.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from ops.models import FeedbackState, PublishState, Report, SourceRun, StageActivity
from ops.runner import StepResult

log = logging.getLogger(__name__)

DEFAULT_DB_PATH = "./pipeline.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS pipeline_runs (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id         TEXT NOT NULL,
    step           TEXT NOT NULL,
    argv           TEXT NOT NULL,
    started_at     TEXT NOT NULL,
    finished_at    TEXT NOT NULL,
    exit_code      INTEGER,
    timed_out      INTEGER NOT NULL DEFAULT 0,
    skipped_reason TEXT,
    stdout_tail    TEXT NOT NULL DEFAULT '',
    stderr_tail    TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_pipeline_runs_step ON pipeline_runs(step, started_at);

CREATE TABLE IF NOT EXISTS health_checks (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    checked_at  TEXT NOT NULL,
    overall     TEXT NOT NULL,
    report_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS alerts_sent (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    check_name TEXT NOT NULL,
    status     TEXT NOT NULL,
    sent_at    TEXT NOT NULL,
    channel    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_alerts_sent_check ON alerts_sent(check_name, sent_at);
"""

OWN_TABLES = ("pipeline_runs", "health_checks", "alerts_sent")


def db_path() -> Path:
    """DB_PATH env var, else config.yaml's db_path, else ./pipeline.db (same as step 2)."""
    env = os.environ.get("DB_PATH")
    if env:
        return Path(env)
    try:
        from config import load_config

        return Path(load_config().get("db_path", DEFAULT_DB_PATH))
    except Exception:  # config.yaml missing or unreadable
        return Path(DEFAULT_DB_PATH)


def connect(path: str | Path | None = None) -> sqlite3.Connection:
    # check_same_thread=False: the control panel opens the connection in a worker thread and
    # may use it on the event loop; each request uses its own connection sequentially, so this
    # is safe. The CLIs are single-threaded and unaffected.
    conn = sqlite3.connect(str(path or db_path()), timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA)
    return conn


def _now() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def _iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC).isoformat()


def _parse(s: Any) -> datetime | None:
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def tables(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
    return {r[0] for r in rows}


def _scalar(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> Any:
    row = conn.execute(sql, params).fetchone()
    return row[0] if row else None


# ---------------------------------------------------------------------------
# Read-only adapters onto other steps' tables
# ---------------------------------------------------------------------------


def configured_sources() -> list[dict[str, Any]]:
    """Names, cadence and enabled flag of every source in the root config.yaml."""
    try:
        from config import load_config

        cfg = load_config()
    except Exception as exc:  # config missing/unreadable: report nothing rather than raise
        log.warning("could not load root config.yaml for the source list: %s", exc)
        return []
    out = []
    for s in cfg.get("sources") or []:
        out.append(
            {
                "name": str(s["name"]),
                "cadence_minutes": int(s.get("cadence_minutes", 60)),
                "enabled": bool(s.get("enabled", True)),
            }
        )
    return out


def configured_backend() -> str:
    """'api' or 'claude_code': LLM_BACKEND env, else the root config.yaml's models.backend.

    Resolved by claude_cli.llm_backend, the one place that rule lives, so the health check
    agrees with what the scorer and drafter actually do. 'api' if it cannot be determined.
    """
    try:
        from claude_cli import llm_backend
        from config import load_config

        return llm_backend(load_config())
    except Exception as exc:  # config missing or a bad value: assume the default backend
        log.warning("could not determine the LLM backend, assuming api: %s", exc)
        return "api"


def fetch_source_runs(
    conn: sqlite3.Connection, sources: list[dict[str, Any]] | None = None
) -> list[SourceRun]:
    """Step 1, as merged on main:
      source_runs(source TEXT PK, last_run_at TEXT, fetched INTEGER, inserted INTEGER,
                  error TEXT)   -- one row per source, overwritten each run
    Joined with the configured source list so an enabled source with no row is
    reported as never ran (last_run_at=None). Sources present only in the table
    (removed from config) are included as disabled.
    """
    if sources is None:
        sources = configured_sources()
    rows: dict[str, sqlite3.Row] = {}
    if "source_runs" in tables(conn):
        for r in conn.execute("SELECT * FROM source_runs"):
            rows[r["source"]] = r
    out: list[SourceRun] = []
    seen: set[str] = set()
    for s in sources:
        r = rows.get(s["name"])
        seen.add(s["name"])
        out.append(
            SourceRun(
                source=s["name"],
                enabled=bool(s.get("enabled", True)),
                cadence_minutes=int(s.get("cadence_minutes", 60)),
                last_run_at=_parse(r["last_run_at"]) if r else None,
                fetched=int(r["fetched"] or 0) if r else 0,
                inserted=int(r["inserted"] or 0) if r else 0,
                error=(r["error"] or None) if r else None,
            )
        )
    for name, r in rows.items():
        if name not in seen:
            out.append(
                SourceRun(
                    source=name,
                    enabled=False,
                    last_run_at=_parse(r["last_run_at"]),
                    fetched=int(r["fetched"] or 0),
                    inserted=int(r["inserted"] or 0),
                    error=r["error"] or None,
                )
            )
    return out


def fetch_stage_activity(conn: sqlite3.Connection, now: datetime | None = None) -> StageActivity:
    """Step 1 + step 2, as merged on main:
      items(id, source, url, doi, title, abstract, published_at, fetched_at, dedup_hash,
            cluster_id, raw_json)
      clusters(id, ..., created_at, prefilter_status 'pass'|'drop'|NULL, prefilter_reason)
      scores(id, cluster_id, model, prompt_version, ..., total, scored_at)
      drafts(id, item_id, cluster_id, model, single_post, thread_json, ..., status
             'pending'|'approved'|'rejected'|'snoozed'|'failed', ..., snoozed_until,
             created_at, updated_at)
      decisions(id, draft_id, action, original_text, edited_text, note, created_at)
    tables_present requires step 1's items/clusters/scores; step 2's tables are optional.
    """
    now = now or _now()
    present = tables(conn)
    act = StageActivity()
    if not {"items", "clusters", "scores"} <= present:
        return act
    act.tables_present = True
    cutoff = _iso(now - timedelta(hours=24))
    act.latest_fetched_at = _parse(_scalar(conn, "SELECT MAX(fetched_at) FROM items"))
    act.latest_scored_at = _parse(_scalar(conn, "SELECT MAX(scored_at) FROM scores"))
    act.unscored_backlog = int(
        _scalar(
            conn,
            """SELECT COUNT(*) FROM clusters c
               WHERE c.prefilter_status = 'pass'
                 AND NOT EXISTS (SELECT 1 FROM scores s WHERE s.cluster_id = c.id)""",
        )
        or 0
    )
    act.scores_24h = int(
        _scalar(conn, "SELECT COUNT(*) FROM scores WHERE scored_at >= ?", (cutoff,)) or 0
    )
    if "drafts" in present:
        act.latest_draft_at = _parse(_scalar(conn, "SELECT MAX(created_at) FROM drafts"))
        act.pending_drafts = int(
            _scalar(conn, "SELECT COUNT(*) FROM drafts WHERE status = 'pending'") or 0
        )
        oldest = _parse(
            _scalar(conn, "SELECT MIN(created_at) FROM drafts WHERE status = 'pending'")
        )
        if oldest is not None:
            act.oldest_pending_age_hours = max(0.0, (now - oldest).total_seconds() / 3600.0)
        act.approved_drafts = int(
            _scalar(conn, "SELECT COUNT(*) FROM drafts WHERE status = 'approved'") or 0
        )
        act.drafts_24h = int(
            _scalar(conn, "SELECT COUNT(*) FROM drafts WHERE created_at >= ?", (cutoff,)) or 0
        )
    if "decisions" in present:
        act.latest_decision_at = _parse(_scalar(conn, "SELECT MAX(created_at) FROM decisions"))
    return act


def fetch_publish_state(conn: sqlite3.Connection, now: datetime | None = None) -> PublishState:
    """Step 3, as merged on main (publish/store.py):
      schedule(id, draft_id UNIQUE, scheduled_for, claimed_at, finished_at,
               status 'pending'|'claimed'|'posted'|'partial'|'refused'|'failed', error)
      posts(id, draft_id, tweet_id, text, kind 'single'|'thread', position, posted_at,
            slot, status 'posted'|'failed', error)
    Only counts and timestamps are read; posts.text is never selected.
    """
    now = now or _now()
    present = tables(conn)
    st = PublishState()
    if not {"schedule", "posts"} <= present:
        return st
    st.tables_present = True
    day = _iso(now - timedelta(hours=24))
    week = _iso(now - timedelta(days=7))
    hour = _iso(now - timedelta(hours=1))
    st.posts_24h = int(
        _scalar(
            conn, "SELECT COUNT(*) FROM posts WHERE status = 'posted' AND posted_at >= ?", (day,)
        )
        or 0
    )
    st.posts_7d = int(
        _scalar(
            conn, "SELECT COUNT(*) FROM posts WHERE status = 'posted' AND posted_at >= ?", (week,)
        )
        or 0
    )
    recent = "COALESCE(finished_at, claimed_at, scheduled_for, '') >= ?"
    st.partial_7d = int(
        _scalar(
            conn, f"SELECT COUNT(*) FROM schedule WHERE status = 'partial' AND {recent}", (week,)
        )
        or 0
    )
    st.failed_7d = int(
        _scalar(
            conn, f"SELECT COUNT(*) FROM schedule WHERE status = 'failed' AND {recent}", (week,)
        )
        or 0
    )
    st.stuck_claimed = int(
        _scalar(
            conn,
            "SELECT COUNT(*) FROM schedule WHERE status = 'claimed' AND claimed_at < ?",
            (hour,),
        )
        or 0
    )
    st.latest_posted_at = _parse(
        _scalar(conn, "SELECT MAX(posted_at) FROM posts WHERE status = 'posted'")
    )
    return st


def fetch_feedback_state(conn: sqlite3.Connection) -> FeedbackState:
    """Step 4, as merged on main (feedback/store.py):
      tweet_metrics(id, tweet_id, draft_id, captured_on, captured_at, ...)
      follower_snapshots(id, captured_on UNIQUE, captured_at, followers, ...)
      feedback_reports(id, window_start, window_end, generated_at, ...)
    tables_present when any of the three exists.
    """
    present = tables(conn)
    st = FeedbackState()
    wanted = {"tweet_metrics", "follower_snapshots", "feedback_reports"}
    if not wanted & present:
        return st
    st.tables_present = True
    if "tweet_metrics" in present:
        v = _scalar(conn, "SELECT MAX(captured_on) FROM tweet_metrics")
        st.latest_captured_on = str(v) if v else None
    if "follower_snapshots" in present:
        v = _scalar(conn, "SELECT MAX(captured_on) FROM follower_snapshots")
        st.latest_snapshot_on = str(v) if v else None
    if "feedback_reports" in present:
        st.latest_report_at = _parse(
            _scalar(conn, "SELECT MAX(generated_at) FROM feedback_reports")
        )
    return st


def fetch_publish_rows(conn: sqlite3.Connection, limit: int = 20) -> dict[str, list[dict]]:
    """Step 3's schedule and posts, row by row, for the control panel's publishing page.

    Read-only like every adapter here, and empty when the tables are absent. `posts.text`
    IS selected: the operator needs to see what went out (or what a partial thread left
    behind). Nothing here writes, retries or clears a row — a partial thread is still a
    human's job, exactly as step 3 documents.
    """
    out: dict[str, list[dict]] = {"posts": [], "needs_attention": []}
    present = tables(conn)
    if not {"schedule", "posts"} <= present:
        return out
    rows = conn.execute(
        """SELECT draft_id, tweet_id, text, kind, position, posted_at, slot, status, error
           FROM posts ORDER BY COALESCE(posted_at, '') DESC, id DESC LIMIT ?""",
        (limit,),
    ).fetchall()
    for r in rows:
        d = dict(r)
        d["posted_at"] = _parse(d["posted_at"])
        out["posts"].append(d)
    stuck = conn.execute(
        """SELECT draft_id, status, scheduled_for, claimed_at, finished_at, error
           FROM schedule WHERE status IN ('partial', 'failed', 'claimed')
           ORDER BY id DESC LIMIT ?""",
        (limit,),
    ).fetchall()
    for r in stuck:
        d = dict(r)
        for key in ("scheduled_for", "claimed_at", "finished_at"):
            d[key] = _parse(d[key])
        out["needs_attention"].append(d)
    return out


def fetch_follower_series(conn: sqlite3.Connection, limit: int = 90) -> list[dict[str, Any]]:
    """Step 4's follower_snapshots, oldest first, at most `limit` days. Empty when absent."""
    if "follower_snapshots" not in tables(conn):
        return []
    rows = conn.execute(
        """SELECT captured_on, followers, following, tweet_count FROM follower_snapshots
           ORDER BY captured_on DESC LIMIT ?""",
        (limit,),
    ).fetchall()
    return [dict(r) for r in reversed(rows)]


def fetch_post_metrics(conn: sqlite3.Connection, limit: int = 20) -> list[dict[str, Any]]:
    """Latest snapshot per tweet from step 4's tweet_metrics, best impressions first."""
    if "tweet_metrics" not in tables(conn):
        return []
    rows = conn.execute(
        """SELECT m.tweet_id, m.draft_id, m.captured_on, m.impressions, m.likes, m.reposts,
                  m.replies, m.quotes, m.bookmarks, m.deleted
           FROM tweet_metrics m
           JOIN (SELECT tweet_id, MAX(id) AS id FROM tweet_metrics GROUP BY tweet_id) latest
             ON latest.id = m.id
           ORDER BY m.impressions DESC, m.tweet_id LIMIT ?""",
        (limit,),
    ).fetchall()
    return [dict(r) for r in rows]


def fetch_latest_feedback_report(conn: sqlite3.Connection) -> dict[str, Any] | None:
    """Step 4's most recent report: its markdown and the changes it PROPOSES.

    The panel renders these; it never applies one. A human edits the rubric or the slots
    and bumps PROMPT_VERSION, exactly as step 4 documents.
    """
    if "feedback_reports" not in tables(conn):
        return None
    r = conn.execute("SELECT * FROM feedback_reports ORDER BY id DESC LIMIT 1").fetchone()
    if r is None:
        return None
    try:
        suggestions = json.loads(r["suggestions_json"] or "[]")
    except ValueError:
        suggestions = []
    return {
        "window_start": r["window_start"],
        "window_end": r["window_end"],
        "generated_at": _parse(r["generated_at"]),
        "report_md": r["report_md"],
        "suggestions": suggestions,
    }


def table_counts(conn: sqlite3.Connection) -> dict[str, int]:
    """Row counts of every user table (read-only; reported, never acted on)."""
    out: dict[str, int] = {}
    for name in sorted(tables(conn)):
        if name.startswith("sqlite_"):
            continue
        out[name] = int(_scalar(conn, f'SELECT COUNT(*) FROM "{name}"') or 0)
    return out


# ---------------------------------------------------------------------------
# Own tables
# ---------------------------------------------------------------------------


def record_results(conn: sqlite3.Connection, run_id: str, results: list[StepResult]) -> None:
    conn.executemany(
        """INSERT INTO pipeline_runs (run_id, step, argv, started_at, finished_at, exit_code,
                                      timed_out, skipped_reason, stdout_tail, stderr_tail)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        [
            (
                run_id,
                r.name,
                json.dumps(r.argv),
                _iso(r.started_at),
                _iso(r.finished_at),
                r.exit_code,
                1 if r.timed_out else 0,
                r.skipped_reason,
                r.stdout_tail,
                r.stderr_tail,
            )
            for r in results
        ],
    )
    conn.commit()


def record_health(conn: sqlite3.Connection, report: Report) -> int:
    cur = conn.execute(
        "INSERT INTO health_checks (checked_at, overall, report_json) VALUES (?, ?, ?)",
        (_iso(report.checked_at), report.overall, json.dumps(report.to_dict())),
    )
    conn.commit()
    return int(cur.lastrowid or 0)


def record_alert(
    conn: sqlite3.Connection, check_name: str, status: str, channel: str, sent_at: datetime
) -> None:
    conn.execute(
        "INSERT INTO alerts_sent (check_name, status, sent_at, channel) VALUES (?, ?, ?, ?)",
        (check_name, status, _iso(sent_at), channel),
    )
    conn.commit()


def last_run_per_step(conn: sqlite3.Connection) -> dict[str, dict[str, Any]]:
    rows = conn.execute(
        """SELECT p.* FROM pipeline_runs p
           JOIN (SELECT step, MAX(id) AS id FROM pipeline_runs GROUP BY step) m ON m.id = p.id
           ORDER BY p.id"""
    ).fetchall()
    out: dict[str, dict[str, Any]] = {}
    for r in rows:
        d = dict(r)
        d["argv"] = json.loads(d["argv"])
        d["started_at"] = _parse(d["started_at"])
        d["finished_at"] = _parse(d["finished_at"])
        d["timed_out"] = bool(d["timed_out"])
        out[d["step"]] = d
    return out


def last_health(conn: sqlite3.Connection) -> dict[str, Any] | None:
    r = conn.execute("SELECT * FROM health_checks ORDER BY id DESC LIMIT 1").fetchone()
    if r is None:
        return None
    return {
        "id": r["id"],
        "checked_at": _parse(r["checked_at"]),
        "overall": r["overall"],
        "report": json.loads(r["report_json"]),
    }


def last_alert_per_check(conn: sqlite3.Connection) -> dict[str, tuple[str, datetime]]:
    rows = conn.execute(
        """SELECT a.check_name, a.status, a.sent_at FROM alerts_sent a
           JOIN (SELECT check_name, MAX(id) AS id FROM alerts_sent GROUP BY check_name) m
             ON m.id = a.id"""
    ).fetchall()
    out: dict[str, tuple[str, datetime]] = {}
    for r in rows:
        when = _parse(r["sent_at"])
        if when is not None:
            out[r["check_name"]] = (r["status"], when)
    return out


def prune_own(conn: sqlite3.Connection, days: int, now: datetime | None = None) -> dict[str, int]:
    """Delete rows older than `days` from the three ops-owned tables only."""
    cutoff = _iso((now or _now()) - timedelta(days=days))
    deleted: dict[str, int] = {}
    for table, col in (
        ("pipeline_runs", "started_at"),
        ("health_checks", "checked_at"),
        ("alerts_sent", "sent_at"),
    ):
        cur = conn.execute(f"DELETE FROM {table} WHERE {col} < ?", (cutoff,))
        deleted[table] = cur.rowcount
    conn.commit()
    return deleted
