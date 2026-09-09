"""Step 4 storage: metric snapshots, follower snapshots, reports, and the read adapters.

Owns three tables, created with CREATE TABLE IF NOT EXISTS in the shared pipeline DB:

  tweet_metrics(id PK, tweet_id, draft_id, captured_on UNIQUE with tweet_id, captured_at,
                impressions, likes, reposts, replies, quotes, bookmarks, deleted)
      One snapshot per tweet per UTC day. A deleted tweet gets one row with deleted=1 and is
      never fetched again.
  follower_snapshots(id PK, captured_on UNIQUE, captured_at, followers, following, tweet_count)
  feedback_reports(id PK, window_start, window_end, generated_at, report_md, suggestions_json)

Never modifies posts, drafts, decisions, items, clusters, scores or ratings.

STEP 3 SCHEMA (publish/store.py, as merged on main). The kickoff prompt assumed a 0-based
`position` and no `status`; the real table is:
  posts(id PK, draft_id, tweet_id, text, kind 'single'|'thread', position (1-based),
        posted_at, slot, status 'posted'|'failed', error)
  Metric-eligible: status='posted' AND tweet_id IS NOT NULL AND error IS NULL. The head of a
  thread is its lowest position.
STEP 2 SCHEMA (approval_queue/store.py):
  drafts(id PK, item_id UNIQUE, cluster_id, model, single_post, thread_json, ...,
         status, ..., created_at, updated_at)   -- no source/url: come from items
  decisions(id PK, draft_id, action, original_text, edited_text, note, created_at)
  A draft "was edited" if any decisions row has edited_text differing from original_text.
STEP 1 SCHEMA (db.py):
  items(id TEXT PK, source, url, doi, title, abstract, published_at, fetched_at, dedup_hash,
        cluster_id, raw_json)
  scores(id PK, cluster_id, model, prompt_version, novelty, clinical_significance,
         audience_interest, expertise_fit, timeliness, evidence_level, hype_risk, total,
         rationale, suggested_angle, raw_response, scored_at)   -- latest row per cluster
  ratings(id PK, cluster_id, rating 1-5, note, rated_at)          -- latest row per cluster
"""

from __future__ import annotations

import json
import os
import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

from feedback.models import (
    DIMENSIONS,
    METRICS,
    FollowerSnapshot,
    Metrics,
    PostContext,
    PostedTweet,
    TweetMetrics,
    TweetSnapshot,
    UserMetrics,
)

DEFAULT_DB_PATH = "./pipeline.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS tweet_metrics (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    tweet_id    TEXT NOT NULL,
    draft_id    INTEGER NOT NULL,
    captured_on TEXT NOT NULL,
    captured_at TEXT NOT NULL,
    impressions INTEGER NOT NULL DEFAULT 0,
    likes       INTEGER NOT NULL DEFAULT 0,
    reposts     INTEGER NOT NULL DEFAULT 0,
    replies     INTEGER NOT NULL DEFAULT 0,
    quotes      INTEGER NOT NULL DEFAULT 0,
    bookmarks   INTEGER NOT NULL DEFAULT 0,
    deleted     INTEGER NOT NULL DEFAULT 0,
    UNIQUE (tweet_id, captured_on)
);
CREATE INDEX IF NOT EXISTS idx_tweet_metrics_draft ON tweet_metrics(draft_id);

CREATE TABLE IF NOT EXISTS follower_snapshots (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    captured_on TEXT NOT NULL UNIQUE,
    captured_at TEXT NOT NULL,
    followers   INTEGER NOT NULL,
    following   INTEGER NOT NULL,
    tweet_count INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS feedback_reports (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    window_start     TEXT NOT NULL,
    window_end       TEXT NOT NULL,
    generated_at     TEXT NOT NULL,
    report_md        TEXT NOT NULL,
    suggestions_json TEXT NOT NULL DEFAULT '[]'
);
"""


def db_path() -> Path:
    """DB_PATH env var, else config.yaml's db_path (same file step 1 uses), else ./pipeline.db."""
    env = os.environ.get("DB_PATH")
    if env:
        return Path(env)
    try:
        from config import load_config

        return Path(load_config().get("db_path", DEFAULT_DB_PATH))
    except Exception:  # config.yaml missing or unreadable
        return Path(DEFAULT_DB_PATH)


def utcnow() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def _iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC).replace(microsecond=0).isoformat()


def _parse(s: str | None) -> datetime | None:
    if not s:
        return None
    dt = datetime.fromisoformat(s)
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def day_of(dt: datetime) -> str:
    """UTC calendar day, YYYY-MM-DD: the key one snapshot per tweet is allowed per."""
    return _parse(_iso(dt)).date().isoformat()


def connect(path: str | Path | None = None) -> sqlite3.Connection:
    """Open the shared pipeline DB and make sure our tables exist."""
    conn = sqlite3.connect(str(path or db_path()), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA)
    return conn


def _tables(conn: sqlite3.Connection) -> set[str]:
    return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}


# ---------------------------------------------------------------------------
# Adapters onto other steps' tables (read-only)
# ---------------------------------------------------------------------------


def fetch_posted(
    since: datetime | None, conn: sqlite3.Connection | None = None
) -> list[PostedTweet]:
    """Every metric-eligible tweet posted at or after `since` (all of them if None).

    This is the ONLY place step 4 reads step 3's posts table. Returns [] if it is absent.
    """
    own = conn is None
    conn = conn or connect()
    try:
        if "posts" not in _tables(conn):
            return []
        sql = (
            "SELECT id, draft_id, tweet_id, kind, position, posted_at, slot FROM posts"
            " WHERE status = 'posted' AND tweet_id IS NOT NULL AND error IS NULL"
            " AND posted_at IS NOT NULL"
        )
        params: tuple[Any, ...] = ()
        if since is not None:
            sql += " AND posted_at >= ?"
            params = (_iso(since),)
        rows = conn.execute(sql + " ORDER BY posted_at, draft_id, position, id", params).fetchall()
    finally:
        if own:
            conn.close()
    return [
        PostedTweet(
            post_id=int(r["id"]),
            draft_id=int(r["draft_id"]),
            tweet_id=str(r["tweet_id"]),
            kind=r["kind"] or "single",
            position=int(r["position"] or 1),
            posted_at=_parse(r["posted_at"]),
            slot=r["slot"],
        )
        for r in rows
    ]


_CONTEXT_SQL = """
SELECT d.id AS draft_id, d.item_id, d.cluster_id,
       i.source, i.url, i.title,
       s.novelty, s.clinical_significance, s.audience_interest, s.expertise_fit, s.timeliness,
       s.hype_risk, s.total, s.evidence_level, s.suggested_angle,
       (SELECT rating FROM ratings WHERE cluster_id = COALESCE(d.cluster_id, i.cluster_id)
          ORDER BY id DESC LIMIT 1) AS rating,
       EXISTS (SELECT 1 FROM decisions x WHERE x.draft_id = d.id
                 AND x.edited_text IS NOT NULL AND x.edited_text != x.original_text) AS edited
FROM drafts d
LEFT JOIN items i ON i.id = d.item_id
LEFT JOIN scores s ON s.id = (SELECT id FROM scores
                              WHERE cluster_id = COALESCE(d.cluster_id, i.cluster_id)
                              ORDER BY id DESC LIMIT 1)
WHERE d.id IN ({placeholders})
"""

_CONTEXT_SQL_NO_STEP1 = """
SELECT d.id AS draft_id, d.item_id, d.cluster_id,
       NULL AS source, NULL AS url, NULL AS title,
       NULL AS novelty, NULL AS clinical_significance, NULL AS audience_interest,
       NULL AS expertise_fit, NULL AS timeliness, NULL AS hype_risk, NULL AS total,
       NULL AS evidence_level, NULL AS suggested_angle, NULL AS rating,
       EXISTS (SELECT 1 FROM decisions x WHERE x.draft_id = d.id
                 AND x.edited_text IS NOT NULL AND x.edited_text != x.original_text) AS edited
FROM drafts d
WHERE d.id IN ({placeholders})
"""


def fetch_post_context(
    draft_ids: Iterable[int], conn: sqlite3.Connection | None = None
) -> dict[int, PostContext]:
    """Story metadata for each draft id, joined from steps 1 and 2.

    This is the ONLY place step 4 reads drafts/decisions/items/scores/ratings. Drafts that
    do not exist are simply absent from the result.
    """
    ids = sorted({int(d) for d in draft_ids})
    if not ids:
        return {}
    own = conn is None
    conn = conn or connect()
    try:
        tables = _tables(conn)
        if not {"drafts", "decisions"} <= tables:
            return {}
        has_step1 = {"items", "scores", "ratings"} <= tables
        template = _CONTEXT_SQL if has_step1 else _CONTEXT_SQL_NO_STEP1
        out: dict[int, PostContext] = {}
        for start in range(0, len(ids), 500):
            chunk = ids[start : start + 500]
            sql = template.format(placeholders=",".join("?" * len(chunk)))
            for r in conn.execute(sql, chunk).fetchall():
                out[int(r["draft_id"])] = _row_to_context(r)
        return out
    finally:
        if own:
            conn.close()


def _row_to_context(r: sqlite3.Row) -> PostContext:
    scores: dict[str, float] = {}
    for k in (*DIMENSIONS, "total"):
        if r[k] is not None:
            scores[k] = float(r[k])
    return PostContext(
        draft_id=int(r["draft_id"]),
        item_id=r["item_id"] or "",
        cluster_id=r["cluster_id"],
        source=r["source"] or "",
        url=r["url"] or "",
        title=r["title"] or "",
        evidence_level=r["evidence_level"] or "",
        scores=scores,
        rating=float(r["rating"]) if r["rating"] is not None else None,
        suggested_angle=r["suggested_angle"] or "",
        edited=bool(r["edited"]),
    )


# ---------------------------------------------------------------------------
# Snapshot schedule
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Schedule:
    daily_for_days: int = 7
    weekly_interval_days: int = 7
    stop_after_days: int = 90

    @classmethod
    def from_config(cls, cfg: dict[str, Any] | None) -> Schedule:
        c = (cfg or {}).get("snapshot") or {}
        return cls(
            daily_for_days=int(c.get("daily_for_days", cls.daily_for_days)),
            weekly_interval_days=int(c.get("weekly_interval_days", cls.weekly_interval_days)),
            stop_after_days=int(c.get("stop_after_days", cls.stop_after_days)),
        )


def snapshot_due(
    posted_at: datetime,
    now: datetime,
    last_captured_on: str | None,
    deleted: bool,
    schedule: Schedule,
) -> bool:
    """Pure schedule rule, in UTC calendar days since posting.

    - deleted tweets: never
    - at most one snapshot per day (a run later the same day fetches nothing)
    - days 0..daily_for_days: every day
    - after that: when the last snapshot is at least weekly_interval_days old
    - after stop_after_days: never
    """
    if deleted:
        return False
    today = date.fromisoformat(day_of(now))
    age = (today - date.fromisoformat(day_of(posted_at))).days
    if age < 0 or age > schedule.stop_after_days:
        return False
    last = date.fromisoformat(last_captured_on) if last_captured_on else None
    if last is not None and last >= today:
        return False
    if age <= schedule.daily_for_days:
        return True
    return last is None or (today - last).days >= schedule.weekly_interval_days


def _snapshot_state(
    conn: sqlite3.Connection, tweet_ids: Iterable[str]
) -> dict[str, tuple[str | None, bool]]:
    """{tweet_id: (last captured_on, ever deleted)} for the given ids."""
    ids = list(tweet_ids)
    out: dict[str, tuple[str | None, bool]] = {}
    for start in range(0, len(ids), 500):
        chunk = ids[start : start + 500]
        rows = conn.execute(
            "SELECT tweet_id, MAX(captured_on) AS last_on, MAX(deleted) AS deleted"
            f" FROM tweet_metrics WHERE tweet_id IN ({','.join('?' * len(chunk))})"
            " GROUP BY tweet_id",
            chunk,
        ).fetchall()
        for r in rows:
            out[str(r["tweet_id"])] = (r["last_on"], bool(r["deleted"]))
    return out


def due_for_snapshot(
    now: datetime,
    conn: sqlite3.Connection | None = None,
    schedule: Schedule | None = None,
    *,
    ignore_schedule: bool = False,
) -> list[PostedTweet]:
    """Tweets whose metrics should be fetched now. Deleted tweets are never returned.

    ignore_schedule (--all) still skips deleted tweets and tweets older than stop_after_days,
    and still fetches at most once per day.
    """
    schedule = schedule or Schedule()
    own = conn is None
    conn = conn or connect()
    try:
        since = now - timedelta(days=schedule.stop_after_days + 1)
        posted = fetch_posted(since, conn)
        state = _snapshot_state(conn, (p.tweet_id for p in posted))
        due: list[PostedTweet] = []
        for p in posted:
            last_on, deleted = state.get(p.tweet_id, (None, False))
            if ignore_schedule:
                age = date.fromisoformat(day_of(now)) - date.fromisoformat(day_of(p.posted_at))
                if deleted or age.days > schedule.stop_after_days or last_on == day_of(now):
                    continue
                due.append(p)
            elif snapshot_due(p.posted_at, now, last_on, deleted, schedule):
                due.append(p)
        return due
    finally:
        if own:
            conn.close()


# ---------------------------------------------------------------------------
# Own tables
# ---------------------------------------------------------------------------


def record_tweet_metrics(
    conn: sqlite3.Connection, draft_id: int, tm: TweetMetrics, now: datetime
) -> None:
    """Upsert today's snapshot for one tweet (deleted tweets store deleted=1 and zeros)."""
    m = Metrics() if tm.deleted else tm.metrics
    conn.execute(
        """INSERT INTO tweet_metrics (tweet_id, draft_id, captured_on, captured_at, impressions,
                                      likes, reposts, replies, quotes, bookmarks, deleted)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(tweet_id, captured_on) DO UPDATE SET
             captured_at = excluded.captured_at, impressions = excluded.impressions,
             likes = excluded.likes, reposts = excluded.reposts, replies = excluded.replies,
             quotes = excluded.quotes, bookmarks = excluded.bookmarks,
             deleted = excluded.deleted""",
        (
            tm.tweet_id,
            draft_id,
            day_of(now),
            _iso(now),
            m.impressions,
            m.likes,
            m.reposts,
            m.replies,
            m.quotes,
            m.bookmarks,
            1 if tm.deleted else 0,
        ),
    )
    conn.commit()


def fetch_tweet_snapshots(
    conn: sqlite3.Connection, tweet_ids: Iterable[str] | None = None
) -> list[TweetSnapshot]:
    """All snapshots (oldest first) for the given tweet ids, or every tweet when None."""
    if tweet_ids is None:
        rows = conn.execute("SELECT * FROM tweet_metrics ORDER BY captured_on, id").fetchall()
    else:
        ids = list(tweet_ids)
        if not ids:
            return []
        rows = []
        for start in range(0, len(ids), 500):
            chunk = ids[start : start + 500]
            rows += conn.execute(
                f"SELECT * FROM tweet_metrics WHERE tweet_id IN ({','.join('?' * len(chunk))})"
                " ORDER BY captured_on, id",
                chunk,
            ).fetchall()
    return [
        TweetSnapshot(
            tweet_id=str(r["tweet_id"]),
            draft_id=int(r["draft_id"]),
            captured_on=r["captured_on"],
            captured_at=_parse(r["captured_at"]),
            metrics=Metrics(**{k: int(r[k] or 0) for k in METRICS}),
            deleted=bool(r["deleted"]),
        )
        for r in rows
    ]


def has_follower_snapshot(conn: sqlite3.Connection, now: datetime) -> bool:
    r = conn.execute(
        "SELECT 1 FROM follower_snapshots WHERE captured_on = ?", (day_of(now),)
    ).fetchone()
    return r is not None


def record_follower_snapshot(conn: sqlite3.Connection, um: UserMetrics, now: datetime) -> None:
    conn.execute(
        """INSERT INTO follower_snapshots (captured_on, captured_at, followers, following,
                                           tweet_count)
           VALUES (?, ?, ?, ?, ?)
           ON CONFLICT(captured_on) DO UPDATE SET captured_at = excluded.captured_at,
             followers = excluded.followers, following = excluded.following,
             tweet_count = excluded.tweet_count""",
        (day_of(now), _iso(now), um.followers, um.following, um.tweet_count),
    )
    conn.commit()


def fetch_follower_snapshots(
    conn: sqlite3.Connection, start: datetime | None = None, end: datetime | None = None
) -> list[FollowerSnapshot]:
    """Follower snapshots with captured_on inside [start, end] (UTC days), oldest first."""
    sql = "SELECT * FROM follower_snapshots"
    clauses, params = [], []
    if start is not None:
        clauses.append("captured_on >= ?")
        params.append(day_of(start))
    if end is not None:
        clauses.append("captured_on <= ?")
        params.append(day_of(end))
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    rows = conn.execute(sql + " ORDER BY captured_on", params).fetchall()
    return [
        FollowerSnapshot(
            captured_on=r["captured_on"],
            captured_at=_parse(r["captured_at"]),
            followers=int(r["followers"]),
            following=int(r["following"]),
            tweet_count=int(r["tweet_count"]),
        )
        for r in rows
    ]


def record_report(
    conn: sqlite3.Connection,
    *,
    window_start: datetime,
    window_end: datetime,
    report_md: str,
    suggestions: list[dict[str, Any]],
    now: datetime | None = None,
) -> int:
    cur = conn.execute(
        """INSERT INTO feedback_reports (window_start, window_end, generated_at, report_md,
                                         suggestions_json)
           VALUES (?, ?, ?, ?, ?)""",
        (
            _iso(window_start),
            _iso(window_end),
            _iso(now or utcnow()),
            report_md,
            json.dumps(suggestions, ensure_ascii=False),
        ),
    )
    conn.commit()
    return int(cur.lastrowid)


def list_reports(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM feedback_reports ORDER BY id").fetchall()
