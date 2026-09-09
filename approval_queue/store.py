"""SQLite storage for drafts and decisions, plus the read-only adapter onto step 1's tables.

Owns two tables (created with CREATE TABLE IF NOT EXISTS in the shared pipeline DB):

  drafts(id INTEGER PK, item_id TEXT UNIQUE, cluster_id, model, single_post, thread_json,
         suggested_visual, why_it_matters, claims_json, status, rejection_reason,
         snoozed_until, created_at, updated_at)
  decisions(id INTEGER PK, draft_id FK, action, original_text, edited_text, note, created_at)

Never modifies the items or scores tables.
"""

from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from draft.schema import Claim, Draft

DEFAULT_DB_PATH = "./pipeline.db"

STATUS_PENDING = "pending"
STATUS_APPROVED = "approved"
STATUS_REJECTED = "rejected"
STATUS_SNOOZED = "snoozed"
STATUS_FAILED = "failed"  # drafter produced output that broke a hard rule
STATUSES = (STATUS_PENDING, STATUS_APPROVED, STATUS_REJECTED, STATUS_SNOOZED, STATUS_FAILED)

ACTION_APPROVE = "approve"
ACTION_EDIT = "edit"
ACTION_REJECT = "reject"
ACTION_SNOOZE = "snooze"
ACTIONS = (ACTION_APPROVE, ACTION_EDIT, ACTION_REJECT, ACTION_SNOOZE)

SNOOZE_HOURS = 24

_SCHEMA = """
CREATE TABLE IF NOT EXISTS drafts (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id          TEXT NOT NULL UNIQUE,
    cluster_id       INTEGER,
    model            TEXT NOT NULL,
    single_post      TEXT NOT NULL,
    thread_json      TEXT NOT NULL,
    suggested_visual TEXT NOT NULL DEFAULT '',
    why_it_matters   TEXT NOT NULL DEFAULT '',
    claims_json      TEXT NOT NULL DEFAULT '[]',
    status           TEXT NOT NULL DEFAULT 'pending',
    rejection_reason TEXT,
    snoozed_until    TEXT,
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_drafts_status ON drafts(status);
CREATE INDEX IF NOT EXISTS idx_drafts_cluster ON drafts(cluster_id);

CREATE TABLE IF NOT EXISTS decisions (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    draft_id      INTEGER NOT NULL REFERENCES drafts(id),
    action        TEXT NOT NULL,
    original_text TEXT NOT NULL,
    edited_text   TEXT,
    note          TEXT,
    created_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_decisions_draft ON decisions(draft_id);
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


def _now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def connect(path: str | Path | None = None) -> sqlite3.Connection:
    """Open the shared pipeline DB and make sure our tables exist."""
    # check_same_thread=False: FastAPI opens the connection in a worker thread and uses it
    # on the event loop; each request uses its connection sequentially, so this is safe.
    conn = sqlite3.connect(str(path or db_path()), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(_SCHEMA)
    return conn


# ---------------------------------------------------------------------------
# Adapter onto step 1's tables
# ---------------------------------------------------------------------------


@dataclass
class Candidate:
    """A scored item ready for drafting. Built only by fetch_candidates()."""

    item_id: str
    cluster_id: int | None
    source: str
    url: str
    title: str
    abstract: str
    published_at: str | None
    total: float
    rationale: str
    suggested_angle: str
    scores: dict[str, float] = field(default_factory=dict)


# STEP 1 SCHEMA (db.py): scores are per *cluster* (one cluster = one story), items carry
# cluster_id. The latest score for a cluster is the one with the highest scores.id.
#   items(id TEXT PK, source, url, doi, title, abstract, published_at, fetched_at,
#         dedup_hash UNIQUE, cluster_id FK, raw_json)
#   clusters(id PK, title, norm_title, doi, published_at, created_at, prefilter_status, ...)
#   scores(id PK, cluster_id FK, model, prompt_version, novelty, clinical_significance,
#          audience_interest, expertise_fit, timeliness, evidence_level, hype_risk, total,
#          rationale, suggested_angle, raw_response, scored_at)
# One candidate per cluster: the member with the longest abstract (best for verifying numbers),
# then earliest published_at, then id.
_CANDIDATES_SQL = """
SELECT i.id, i.cluster_id, i.source, i.url, i.title, i.abstract, i.published_at,
       s.novelty, s.clinical_significance, s.audience_interest, s.expertise_fit,
       s.timeliness, s.total, s.rationale, s.suggested_angle
FROM clusters c
JOIN scores s ON s.id = (SELECT id FROM scores WHERE cluster_id = c.id ORDER BY id DESC LIMIT 1)
JOIN items i ON i.id = (SELECT id FROM items WHERE cluster_id = c.id
                        ORDER BY LENGTH(abstract) DESC, published_at IS NULL, published_at, id
                        LIMIT 1)
WHERE s.total >= :min_score
  AND s.scored_at >= :since
ORDER BY s.total DESC, s.scored_at DESC
"""


def fetch_candidates(
    min_score: float, since_hours: float, conn: sqlite3.Connection | None = None
) -> list[Candidate]:
    """Return items scored at or above min_score within the last since_hours.

    This is the ONLY place step 2 reads the items/scores tables.
    """
    own = conn is None
    conn = conn or connect()
    try:
        since = (datetime.now(UTC) - timedelta(hours=since_hours)).replace(microsecond=0)
        rows = conn.execute(
            _CANDIDATES_SQL, {"min_score": min_score, "since": since.isoformat()}
        ).fetchall()
    finally:
        if own:
            conn.close()
    out: list[Candidate] = []
    for r in rows:
        out.append(
            Candidate(
                item_id=r["id"],
                cluster_id=r["cluster_id"],
                source=r["source"] or "",
                url=r["url"] or "",
                title=r["title"] or "",
                abstract=r["abstract"] or "",
                published_at=r["published_at"],
                total=float(r["total"] or 0),
                rationale=r["rationale"] or "",
                suggested_angle=r["suggested_angle"] or "",
                scores={
                    k: float(r[k] or 0)
                    for k in (
                        "novelty",
                        "clinical_significance",
                        "audience_interest",
                        "expertise_fit",
                        "timeliness",
                    )
                },
            )
        )
    return out


# ---------------------------------------------------------------------------
# Drafts
# ---------------------------------------------------------------------------


@dataclass
class DraftRow:
    id: int
    item_id: str
    cluster_id: int | None
    model: str
    draft: Draft
    status: str
    rejection_reason: str | None
    snoozed_until: str | None
    created_at: str
    updated_at: str
    # Joined from items/scores when available (None if step 1 tables are absent)
    source: str = ""
    url: str = ""
    title: str = ""
    abstract: str = ""
    total: float | None = None
    rationale: str = ""
    suggested_angle: str = ""


def _row_to_draft(r: sqlite3.Row) -> DraftRow:
    keys = r.keys()
    draft = Draft(
        single_post=r["single_post"],
        thread=json.loads(r["thread_json"]),
        suggested_visual=r["suggested_visual"],
        why_it_matters=r["why_it_matters"],
        claims_to_verify=[Claim(**c) for c in json.loads(r["claims_json"])],
    )
    return DraftRow(
        id=r["id"],
        item_id=r["item_id"],
        cluster_id=r["cluster_id"],
        model=r["model"],
        draft=draft,
        status=r["status"],
        rejection_reason=r["rejection_reason"],
        snoozed_until=r["snoozed_until"],
        created_at=r["created_at"],
        updated_at=r["updated_at"],
        source=(r["source"] or "") if "source" in keys else "",
        url=(r["url"] or "") if "url" in keys else "",
        title=(r["title"] or "") if "title" in keys else "",
        abstract=(r["abstract"] or "") if "abstract" in keys else "",
        total=(float(r["total"]) if r["total"] is not None else None) if "total" in keys else None,
        rationale=(r["rationale"] or "") if "rationale" in keys else "",
        suggested_angle=(r["suggested_angle"] or "") if "suggested_angle" in keys else "",
    )


def has_draft(conn: sqlite3.Connection, item_id: str, cluster_id: int | None = None) -> bool:
    """True if this item, or any item in the same cluster (same story), already has a draft."""
    if cluster_id is None:
        row = conn.execute("SELECT 1 FROM drafts WHERE item_id = ?", (item_id,)).fetchone()
    else:
        row = conn.execute(
            "SELECT 1 FROM drafts WHERE item_id = ? OR cluster_id = ?", (item_id, cluster_id)
        ).fetchone()
    return row is not None


def insert_draft(
    conn: sqlite3.Connection,
    *,
    item_id: str,
    model: str,
    draft: Draft,
    cluster_id: int | None = None,
    status: str = STATUS_PENDING,
    rejection_reason: str | None = None,
) -> int:
    if status not in STATUSES:
        raise ValueError(f"unknown status {status!r}")
    now = _now()
    cur = conn.execute(
        """INSERT INTO drafts (item_id, cluster_id, model, single_post, thread_json,
                               suggested_visual, why_it_matters, claims_json, status,
                               rejection_reason, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            item_id,
            cluster_id,
            model,
            draft.single_post,
            json.dumps(draft.thread),
            draft.suggested_visual,
            draft.why_it_matters,
            json.dumps([c.__dict__ for c in draft.claims_to_verify]),
            status,
            rejection_reason,
            now,
            now,
        ),
    )
    conn.commit()
    return int(cur.lastrowid)


def step1_tables_present(conn: sqlite3.Connection) -> bool:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' "
        "AND name IN ('items','clusters','scores')"
    ).fetchall()
    return len(rows) == 3


def _select_drafts_sql(conn: sqlite3.Connection) -> str:
    if step1_tables_present(conn):
        return """
        SELECT d.*, i.source, i.url, i.title, i.abstract,
               s.total, s.rationale, s.suggested_angle
        FROM drafts d
        LEFT JOIN items i ON i.id = d.item_id
        LEFT JOIN scores s ON s.id = (SELECT id FROM scores WHERE cluster_id = i.cluster_id
                                      ORDER BY id DESC LIMIT 1)
        """
    return "SELECT d.* FROM drafts d"


def get_draft(conn: sqlite3.Connection, draft_id: int) -> DraftRow | None:
    r = conn.execute(_select_drafts_sql(conn) + " WHERE d.id = ?", (draft_id,)).fetchone()
    return _row_to_draft(r) if r else None


def list_drafts(conn: sqlite3.Connection, status: str = STATUS_PENDING) -> list[DraftRow]:
    """Drafts in a status. For 'pending', snoozed drafts whose snooze has expired are included."""
    if status == STATUS_PENDING:
        where = (
            " WHERE d.status = 'pending'"
            " OR (d.status = 'snoozed' AND d.snoozed_until IS NOT NULL AND d.snoozed_until <= ?)"
        )
        params: tuple[Any, ...] = (_now(),)
    else:
        where = " WHERE d.status = ?"
        params = (status,)
    order = " ORDER BY d.created_at DESC, d.id DESC"
    rows = conn.execute(_select_drafts_sql(conn) + where + order, params).fetchall()
    return [_row_to_draft(r) for r in rows]


def _set_status(
    conn: sqlite3.Connection, draft_id: int, status: str, *, snoozed_until: str | None = None
) -> None:
    conn.execute(
        "UPDATE drafts SET status = ?, snoozed_until = ?, updated_at = ? WHERE id = ?",
        (status, snoozed_until, _now(), draft_id),
    )


def _record_decision(
    conn: sqlite3.Connection,
    draft_id: int,
    action: str,
    original_text: str,
    edited_text: str | None,
    note: str | None,
) -> int:
    cur = conn.execute(
        """INSERT INTO decisions (draft_id, action, original_text, edited_text, note, created_at)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (draft_id, action, original_text, edited_text, note, _now()),
    )
    return int(cur.lastrowid)


def _require(conn: sqlite3.Connection, draft_id: int) -> DraftRow:
    row = get_draft(conn, draft_id)
    if row is None:
        raise KeyError(f"no draft with id {draft_id}")
    return row


def approve(conn: sqlite3.Connection, draft_id: int, note: str | None = None) -> int:
    """Mark approved. Nothing is published here; that is step 3."""
    row = _require(conn, draft_id)
    _set_status(conn, draft_id, STATUS_APPROVED)
    did = _record_decision(conn, draft_id, ACTION_APPROVE, row.draft.single_post, None, note)
    conn.commit()
    return did


def edit(
    conn: sqlite3.Connection,
    draft_id: int,
    *,
    single_post: str,
    thread: list[str],
    note: str | None = None,
    approve_after: bool = True,
) -> int:
    """Save edited text over the draft and log original vs edited for voice-guide training."""
    row = _require(conn, draft_id)
    original = _serialise_text(row.draft.single_post, row.draft.thread)
    edited = _serialise_text(single_post, thread)
    conn.execute(
        "UPDATE drafts SET single_post = ?, thread_json = ?, updated_at = ? WHERE id = ?",
        (single_post, json.dumps(thread), _now(), draft_id),
    )
    if approve_after:
        _set_status(conn, draft_id, STATUS_APPROVED)
    did = _record_decision(conn, draft_id, ACTION_EDIT, original, edited, note)
    conn.commit()
    return did


def reject(conn: sqlite3.Connection, draft_id: int, note: str | None = None) -> int:
    row = _require(conn, draft_id)
    _set_status(conn, draft_id, STATUS_REJECTED)
    did = _record_decision(conn, draft_id, ACTION_REJECT, row.draft.single_post, None, note)
    conn.commit()
    return did


def snooze(
    conn: sqlite3.Connection, draft_id: int, hours: float = SNOOZE_HOURS, note: str | None = None
) -> int:
    """Hide the draft for `hours`; it reappears in the pending list afterwards."""
    row = _require(conn, draft_id)
    until = (datetime.now(UTC) + timedelta(hours=hours)).replace(microsecond=0).isoformat()
    _set_status(conn, draft_id, STATUS_SNOOZED, snoozed_until=until)
    did = _record_decision(conn, draft_id, ACTION_SNOOZE, row.draft.single_post, None, note)
    conn.commit()
    return did


def list_decisions(conn: sqlite3.Connection, draft_id: int | None = None) -> list[sqlite3.Row]:
    if draft_id is None:
        return conn.execute("SELECT * FROM decisions ORDER BY id").fetchall()
    return conn.execute(
        "SELECT * FROM decisions WHERE draft_id = ? ORDER BY id", (draft_id,)
    ).fetchall()


def _serialise_text(single_post: str, thread: list[str]) -> str:
    """Canonical text form of a draft for the decisions log: JSON so it round-trips."""
    return json.dumps({"single_post": single_post, "thread": thread}, ensure_ascii=False)
