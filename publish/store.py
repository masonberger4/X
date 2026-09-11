"""Step 3 storage: the posts log and the schedule/claim table, plus the read adapter.

Owns two tables, created with CREATE TABLE IF NOT EXISTS in the shared pipeline DB:

  schedule(id PK, draft_id UNIQUE, scheduled_for, claimed_at, finished_at,
           status 'pending'|'claimed'|'posted'|'partial'|'refused'|'failed', error)
      One row per draft we ever tried to publish. Claiming (claimed_at) happens inside a
      BEGIN IMMEDIATE transaction before any API call, so two overlapping cron runs cannot
      both post the same draft.
  posts(id PK, draft_id, tweet_id, text, kind 'single'|'thread', position, posted_at, slot,
        status 'posted'|'failed', error)
      One row per tweet attempted. A thread that failed at post k has rows 1..k-1 with
      tweet_ids and row k with status='failed' and the error; schedule.status is 'partial'.

Never modifies drafts, decisions, items, clusters or scores.

STEP 2 SCHEMA (approval_queue/store.py, as merged on main; the kickoff prompt assumed a
simpler shape with source/url columns on drafts, which is reconciled here):
  drafts(id PK, item_id UNIQUE, cluster_id, model, single_post, thread_json,
         suggested_visual, why_it_matters, claims_json, status, rejection_reason,
         snoozed_until, created_at, updated_at)
  decisions(id PK, draft_id FK, action 'approve'|'edit'|'reject'|'snooze'|'revise',
            original_text, edited_text, note, created_at)
      edited_text is JSON {"single_post": ..., "thread": [...]} (a plain string is also
      accepted here as a single_post edit).
STEP 1 SCHEMA (db.py): items(id, source, url, title, cluster_id, ...),
  scores(cluster_id, total, ...). Used only to fill source/url/title/score.
"""

from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from draft.chart import IMAGES_DIRNAME, alt_text, visual_from_json

DEFAULT_DB_PATH = "./pipeline.db"

KIND_SINGLE = "single"
KIND_THREAD = "thread"

SCHED_PENDING = "pending"
SCHED_CLAIMED = "claimed"
SCHED_POSTED = "posted"
SCHED_PARTIAL = "partial"
SCHED_REFUSED = "refused"
SCHED_FAILED = "failed"

POST_POSTED = "posted"
POST_FAILED = "failed"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS schedule (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    draft_id      INTEGER NOT NULL UNIQUE,
    scheduled_for TEXT,
    claimed_at    TEXT,
    finished_at   TEXT,
    status        TEXT NOT NULL DEFAULT 'pending',
    error         TEXT
);
CREATE INDEX IF NOT EXISTS idx_schedule_status ON schedule(status);

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
);
CREATE INDEX IF NOT EXISTS idx_posts_draft ON posts(draft_id);
CREATE INDEX IF NOT EXISTS idx_posts_posted_at ON posts(posted_at);
"""


def db_path() -> Path:
    return Path(os.environ.get("DB_PATH") or DEFAULT_DB_PATH)


def _now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def connect(path: str | Path | None = None) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path or db_path()), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA)
    return conn


# ---------------------------------------------------------------------------
# Adapter onto step 2 (and, for metadata, step 1)
# ---------------------------------------------------------------------------


@dataclass
class Approved:
    """An approved draft ready to publish. Built only by fetch_approved()."""

    draft_id: int
    item_id: str
    cluster_id: int | None
    source: str
    url: str
    title: str
    single_post: str
    thread: list[str] = field(default_factory=list)
    score: float | None = None
    approved_at: str | None = None
    edited: bool = False
    image_path: str | None = None  # absolute path of the rendered chart PNG, if still on disk
    image_alt: str = ""


def _db_file(conn: sqlite3.Connection) -> Path | None:
    """The file behind a connection (None for :memory:), so image paths resolve next to it."""
    for row in conn.execute("PRAGMA database_list"):
        if row[1] == "main":
            return Path(row[2]) if row[2] else None
    return None


def _image_for(
    db_file: Path | None,
    image_path: str | None,
    chart_json: str | None,
    url: str,
    image_alt: str | None = None,
) -> tuple[str | None, str]:
    """(absolute PNG path if it exists, alt text) for an approved draft. Images live in
    <db folder>/images (approval_queue.store.image_dir); the row stores the relative path.
    The stored alt text (written with the picture) wins; rows from before it existed get
    the alt text of their chart spec."""
    if not image_path or db_file is None:
        return None, ""
    path = db_file.resolve().parent / IMAGES_DIRNAME / image_path
    if not path.is_file():
        return None, ""
    if image_alt:
        return str(path), image_alt
    visual = visual_from_json(chart_json)
    return str(path), (alt_text(visual, url) if visual else "")


def _tables(conn: sqlite3.Connection) -> set[str]:
    return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def _apply_edit(edited_text: str | None, single_post: str, thread: list[str]):
    """Prefer the human's edited text. Returns (single_post, thread, edited?)."""
    if not edited_text:
        return single_post, thread, False
    try:
        data = json.loads(edited_text)
    except json.JSONDecodeError:
        return edited_text, thread, True
    if isinstance(data, dict):
        sp = data.get("single_post") or single_post
        th = data.get("thread")
        th = th if isinstance(th, list) and all(isinstance(p, str) for p in th) else thread
        return sp, th, True
    return edited_text, thread, True


def fetch_approved(limit: int = 10, conn: sqlite3.Connection | None = None) -> list[Approved]:
    """Approved drafts that have not been claimed for publishing, oldest approval first.

    This is the ONLY place step 3 reads the drafts/decisions (and items/scores) tables.
    """
    own = conn is None
    conn = conn or connect()
    conn_path = _db_file(conn)
    try:
        conn.executescript(_SCHEMA)  # the caller may hand us step 2's connection
        tables = _tables(conn)
        if "drafts" not in tables:
            return []
        has_step1 = {"items", "scores"} <= tables
        draft_cols = {r[1] for r in conn.execute("PRAGMA table_info(drafts)").fetchall()}
        image_cols = (
            ", d.chart_json, d.image_path"
            if {"chart_json", "image_path"} <= draft_cols
            else ", NULL AS chart_json, NULL AS image_path"
        )
        image_cols += ", d.image_alt" if "image_alt" in draft_cols else ", NULL AS image_alt"
        meta = (
            ", i.source, i.url, i.title, s.total"
            if has_step1
            else ", NULL AS source, NULL AS url, NULL AS title, NULL AS total"
        )
        joins = (
            " LEFT JOIN items i ON i.id = d.item_id"
            " LEFT JOIN scores s ON s.id = (SELECT id FROM scores WHERE cluster_id = i.cluster_id"
            "                               ORDER BY id DESC LIMIT 1)"
            if has_step1
            else ""
        )
        rows = conn.execute(
            f"""
            SELECT d.id, d.item_id, d.cluster_id, d.single_post, d.thread_json, d.updated_at,
                   (SELECT edited_text FROM decisions WHERE draft_id = d.id
                      AND action IN ('edit', 'revise') AND edited_text IS NOT NULL
                      ORDER BY id DESC LIMIT 1) AS edited_text,
                   (SELECT created_at FROM decisions WHERE draft_id = d.id
                      AND action IN ('approve', 'edit') ORDER BY id DESC LIMIT 1) AS approved_at
                   {meta}{image_cols}
            FROM drafts d {joins}
            WHERE d.status = 'approved'
              AND d.id NOT IN (SELECT draft_id FROM schedule WHERE claimed_at IS NOT NULL)
            ORDER BY approved_at, d.id
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    finally:
        if own:
            conn.close()
    out: list[Approved] = []
    for r in rows:
        try:
            thread = json.loads(r["thread_json"] or "[]")
        except json.JSONDecodeError:
            thread = []
        single_post, thread, edited = _apply_edit(r["edited_text"], r["single_post"], thread)
        image_path, image_alt = _image_for(
            conn_path, r["image_path"], r["chart_json"], r["url"] or "", r["image_alt"]
        )
        out.append(
            Approved(
                draft_id=int(r["id"]),
                item_id=r["item_id"],
                cluster_id=r["cluster_id"],
                source=r["source"] or "",
                url=r["url"] or "",
                title=r["title"] or "",
                single_post=single_post,
                thread=thread,
                score=float(r["total"]) if r["total"] is not None else None,
                approved_at=r["approved_at"] or r["updated_at"],
                edited=edited,
                image_path=image_path,
                image_alt=image_alt,
            )
        )
    return out


# ---------------------------------------------------------------------------
# Schedule / claim
# ---------------------------------------------------------------------------


def claim(conn: sqlite3.Connection, draft_id: int, scheduled_for: str | None = None) -> bool:
    """Atomically claim a draft for posting. False if another run already claimed it."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute(
            "INSERT OR IGNORE INTO schedule (draft_id, scheduled_for, status) VALUES (?, ?, ?)",
            (draft_id, scheduled_for, SCHED_PENDING),
        )
        cur = conn.execute(
            "UPDATE schedule SET claimed_at = ?, status = ?,"
            " scheduled_for = COALESCE(?, scheduled_for)"
            " WHERE draft_id = ? AND claimed_at IS NULL",
            (_now(), SCHED_CLAIMED, scheduled_for, draft_id),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return cur.rowcount == 1


def finish(conn: sqlite3.Connection, draft_id: int, status: str, error: str | None = None) -> None:
    conn.execute(
        "UPDATE schedule SET status = ?, error = ?, finished_at = ? WHERE draft_id = ?",
        (status, error, _now(), draft_id),
    )
    conn.commit()


def get_schedule(conn: sqlite3.Connection, draft_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM schedule WHERE draft_id = ?", (draft_id,)).fetchone()


def release_failed(conn: sqlite3.Connection, draft_ids: list[int] | None = None) -> list[int]:
    """Delete the schedule rows of drafts whose publish attempt failed BEFORE anything went
    live ('failed' or 'refused', and no posts row carries a tweet_id), so fetch_approved picks
    them up again; the failed posts log rows stay as history. 'posted' and 'partial' rows are
    never touched: a partial thread is live on X and stays a human decision. With draft_ids,
    only those drafts; else every eligible row. Returns the released draft ids."""
    where = f"AND draft_id IN ({','.join('?' * len(draft_ids))})" if draft_ids else ""
    ids = [
        int(r[0])
        for r in conn.execute(
            f"""SELECT draft_id FROM schedule
                WHERE status IN (?, ?)
                  AND draft_id NOT IN (SELECT draft_id FROM posts WHERE tweet_id IS NOT NULL)
                  {where}
                ORDER BY draft_id""",
            [SCHED_FAILED, SCHED_REFUSED, *(draft_ids or [])],
        ).fetchall()
    ]
    if ids:
        conn.execute(f"DELETE FROM schedule WHERE draft_id IN ({','.join('?' * len(ids))})", ids)
        conn.commit()
    return ids


# ---------------------------------------------------------------------------
# Posts log
# ---------------------------------------------------------------------------


def record_post(
    conn: sqlite3.Connection,
    *,
    draft_id: int,
    text: str,
    kind: str,
    position: int,
    slot: str | None,
    tweet_id: str | None = None,
    error: str | None = None,
    posted_at: datetime | None = None,
) -> int:
    """Log one attempted tweet. posted_at defaults to now (UTC) for successful posts."""
    status = POST_POSTED if tweet_id else POST_FAILED
    when = None
    if tweet_id:
        when = (
            (posted_at.astimezone(UTC) if posted_at else datetime.now(UTC))
            .replace(microsecond=0)
            .isoformat()
        )
    cur = conn.execute(
        """INSERT INTO posts (draft_id, tweet_id, text, kind, position, posted_at, slot, status,
                              error)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            draft_id,
            tweet_id,
            text,
            kind,
            position,
            when,
            slot,
            status,
            error,
        ),
    )
    conn.commit()
    return int(cur.lastrowid)


def list_posts(conn: sqlite3.Connection, draft_id: int | None = None) -> list[sqlite3.Row]:
    if draft_id is None:
        return conn.execute("SELECT * FROM posts ORDER BY id").fetchall()
    return conn.execute(
        "SELECT * FROM posts WHERE draft_id = ? ORDER BY position", (draft_id,)
    ).fetchall()


def posted_since(conn: sqlite3.Connection, since: datetime) -> list[sqlite3.Row]:
    """Successful posts at or after `since` (aware). Threads count once (position 1)."""
    return conn.execute(
        "SELECT * FROM posts WHERE status = 'posted' AND position = 1 AND posted_at >= ?"
        " ORDER BY posted_at",
        (since.astimezone(UTC).replace(microsecond=0).isoformat(),),
    ).fetchall()


def last_posted_at(conn: sqlite3.Connection) -> datetime | None:
    r = conn.execute("SELECT MAX(posted_at) AS t FROM posts WHERE status = 'posted'").fetchone()
    return datetime.fromisoformat(r["t"]) if r and r["t"] else None


def slot_used(conn: sqlite3.Connection, slot: str) -> bool:
    r = conn.execute("SELECT 1 FROM posts WHERE slot = ? AND status = 'posted' LIMIT 1", (slot,))
    return r.fetchone() is not None
