"""SQLite storage for drafts and decisions, plus the read-only adapter onto step 1's tables.

Owns two tables (created with CREATE TABLE IF NOT EXISTS in the shared pipeline DB):

  drafts(id INTEGER PK, item_id TEXT UNIQUE, cluster_id, model, single_post, thread_json,
         suggested_visual, why_it_matters, claims_json, status, rejection_reason,
         snoozed_until, created_at, updated_at,
         chart_json, image_path)   -- added by guarded migrations: the drafter's chart spec
            -- (draft/chart.py) and the PNG rendered from it, relative to image_dir()
  decisions(id INTEGER PK, draft_id FK, action, original_text, edited_text, note, created_at,
            category)   -- category added by step 7 through a guarded ALTER TABLE migration
            -- action 'revise': the drafter rewrote the text on the human's instructions
            -- (note); original_text/edited_text hold the before/after like an 'edit'.
  draft_examples(id INTEGER PK, draft_id FK, decision_id FK, kind 'edit'|'rejection',
                 created_at)   -- step 7: which examples each draft was shown

Never modifies the items or scores tables. Step 7 reads items only through
fetch_decisions_for_voice / fetch_draft_stats, and only for source and url.
"""

from __future__ import annotations

import json
import os
import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from draft.chart import (
    IMAGES_DIRNAME,
    Chart,
    Table,
    chart_from_json,
    table_from_json,
    visual_from_json,
)
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
ACTION_REVISE = "revise"  # the drafter rewrote the draft on the human's instructions
ACTIONS = (ACTION_APPROVE, ACTION_EDIT, ACTION_REJECT, ACTION_SNOOZE, ACTION_REVISE)

SNOOZE_HOURS = 24

# Step 7: why a draft was edited or rejected. Stored in decisions.category (nullable).
CATEGORY_VOICE = "voice"
CATEGORY_FACTUAL = "factual"
CATEGORY_NOT_NEWSWORTHY = "not_newsworthy"
CATEGORY_HARD_RULE = "hard_rule"
CATEGORY_OTHER = "other"
DECISION_CATEGORIES = (
    CATEGORY_VOICE,
    CATEGORY_FACTUAL,
    CATEGORY_NOT_NEWSWORTHY,
    CATEGORY_HARD_RULE,
    CATEGORY_OTHER,
)

EXAMPLE_KIND_EDIT = "edit"
EXAMPLE_KIND_REJECTION = "rejection"
EXAMPLE_KINDS = (EXAMPLE_KIND_EDIT, EXAMPLE_KIND_REJECTION)

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
    created_at    TEXT NOT NULL,
    category      TEXT
);
CREATE INDEX IF NOT EXISTS idx_decisions_draft ON decisions(draft_id);

CREATE TABLE IF NOT EXISTS draft_examples (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    draft_id    INTEGER NOT NULL REFERENCES drafts(id),
    decision_id INTEGER NOT NULL REFERENCES decisions(id),
    kind        TEXT NOT NULL,
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_draft_examples_draft ON draft_examples(draft_id);
CREATE INDEX IF NOT EXISTS idx_draft_examples_decision ON draft_examples(decision_id);

CREATE TABLE IF NOT EXISTS image_grades (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    draft_id         INTEGER NOT NULL REFERENCES drafts(id),
    iteration        INTEGER NOT NULL,
    score            INTEGER NOT NULL,
    flaws_json       TEXT NOT NULL DEFAULT '[]',
    fixes_json       TEXT NOT NULL DEFAULT '[]',
    adjustments_json TEXT NOT NULL DEFAULT '{}',
    style_json       TEXT NOT NULL DEFAULT '{}',
    model            TEXT NOT NULL DEFAULT '',
    kept             INTEGER NOT NULL DEFAULT 0,
    created_at       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_image_grades_draft ON image_grades(draft_id);
"""

# Guarded, additive migrations for databases created before a column existed. Each entry is
# (table, column, ALTER TABLE statement). Never a destructive rebuild: steps 4 and 5 read
# decisions through adapters that select named columns and must keep working on old and new
# databases alike.
_MIGRATIONS = (
    ("decisions", "category", "ALTER TABLE decisions ADD COLUMN category TEXT"),
    ("drafts", "chart_json", "ALTER TABLE drafts ADD COLUMN chart_json TEXT"),
    ("drafts", "image_path", "ALTER TABLE drafts ADD COLUMN image_path TEXT"),
    ("drafts", "image_alt", "ALTER TABLE drafts ADD COLUMN image_alt TEXT"),
)


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def _migrate(conn: sqlite3.Connection) -> None:
    for table, column, ddl in _MIGRATIONS:
        if column not in _columns(conn, table):
            conn.execute(ddl)
    conn.commit()


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


def image_dir() -> Path:
    """Where rendered draft images live: an `images` folder next to the pipeline DB.
    drafts.image_path is stored relative to it, so the data folder can move."""
    return db_path().resolve().parent / IMAGES_DIRNAME


def image_file(draft_id: int) -> Path:
    return image_dir() / f"draft_{draft_id}.png"


def resolve_image(image_path: str | None) -> Path | None:
    """Absolute path of a draft's image, or None when there is none or the file is gone."""
    if not image_path:
        return None
    path = image_dir() / image_path
    return path if path.is_file() else None


def _now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def _chart_json(draft: Draft) -> str | None:
    """The draft's visual (chart or table) as stored in drafts.chart_json."""
    return json.dumps(draft.visual.to_dict()) if draft.visual is not None else None


def connect(path: str | Path | None = None) -> sqlite3.Connection:
    """Open the shared pipeline DB and make sure our tables exist."""
    # check_same_thread=False: FastAPI opens the connection in a worker thread and uses it
    # on the event loop; each request uses its connection sequentially, so this is safe.
    conn = sqlite3.connect(str(path or db_path()), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(_SCHEMA)
    _migrate(conn)
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
    image_path: str | None = None  # relative to image_dir(); None: no image
    image_alt: str = ""  # alt text of the rendered picture (set with image_path)


def _row_to_draft(r: sqlite3.Row) -> DraftRow:
    keys = r.keys()
    draft = Draft(
        single_post=r["single_post"],
        thread=json.loads(r["thread_json"]),
        suggested_visual=r["suggested_visual"],
        why_it_matters=r["why_it_matters"],
        claims_to_verify=[Claim(**c) for c in json.loads(r["claims_json"])],
        chart=chart_from_json(r["chart_json"]) if "chart_json" in keys else None,
        table=table_from_json(r["chart_json"]) if "chart_json" in keys else None,
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
        image_path=(r["image_path"] or None) if "image_path" in keys else None,
        image_alt=(r["image_alt"] or "") if "image_alt" in keys else "",
    )


def has_draft(
    conn: sqlite3.Connection,
    item_id: str,
    cluster_id: int | None = None,
    *,
    ignore_failed: bool = False,
) -> bool:
    """True if this item, or any item in the same cluster (same story), already has a draft.
    With ignore_failed, drafts that failed the hard rules do not count (run_draft
    --retry-failed)."""
    where = "item_id = ?" if cluster_id is None else "(item_id = ? OR cluster_id = ?)"
    params: tuple = (item_id,) if cluster_id is None else (item_id, cluster_id)
    if ignore_failed:
        where += " AND status != ?"
        params += (STATUS_FAILED,)
    row = conn.execute(f"SELECT 1 FROM drafts WHERE {where}", params).fetchone()
    return row is not None


def delete_failed_drafts(
    conn: sqlite3.Connection, item_id: str, cluster_id: int | None = None
) -> int:
    """Remove the failed drafts for a story (and their draft_examples rows) so run_draft
    --retry-failed can insert a fresh one; drafts.item_id is UNIQUE. Returns rows removed."""
    where = "item_id = ?" if cluster_id is None else "(item_id = ? OR cluster_id = ?)"
    params: tuple = (item_id,) if cluster_id is None else (item_id, cluster_id)
    ids = [
        r[0]
        for r in conn.execute(
            f"SELECT id FROM drafts WHERE {where} AND status = ?", params + (STATUS_FAILED,)
        )
    ]
    for draft_id in ids:
        conn.execute("DELETE FROM draft_examples WHERE draft_id = ?", (draft_id,))
        conn.execute("DELETE FROM drafts WHERE id = ?", (draft_id,))
    conn.commit()
    return len(ids)


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
                               rejection_reason, created_at, updated_at, chart_json)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
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
            _chart_json(draft),
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


@dataclass
class PublishInfo:
    """What step 3 did with a draft, read from its schedule/posts tables (never written here)."""

    status: str  # posted | partial | failed | refused | claimed | pending
    tweet_id: str | None = None  # first post's tweet id, when it is live
    posted_at: str | None = None
    error: str | None = None

    @property
    def tweet_url(self) -> str:
        return f"https://x.com/i/web/status/{self.tweet_id}" if self.tweet_id else ""


def publish_states(conn: sqlite3.Connection, draft_ids: list[int]) -> dict[int, PublishInfo]:
    """Read-only look at step 3's `schedule` and `posts` for these drafts, so the approved
    page can say which of them already went out. Empty when the tables do not exist yet (no
    publish run so far) or for a draft step 3 never claimed."""
    if not draft_ids:
        return {}
    present = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if not {"schedule", "posts"} <= present:
        return {}
    marks = ",".join("?" * len(draft_ids))
    out: dict[int, PublishInfo] = {}
    for r in conn.execute(
        f"SELECT draft_id, status, error FROM schedule WHERE draft_id IN ({marks})", draft_ids
    ).fetchall():
        out[int(r["draft_id"])] = PublishInfo(status=str(r["status"]), error=r["error"])
    for r in conn.execute(
        f"""SELECT draft_id, tweet_id, posted_at FROM posts
            WHERE position = 1 AND tweet_id IS NOT NULL AND draft_id IN ({marks})""",
        draft_ids,
    ).fetchall():
        info = out.get(int(r["draft_id"]))
        if info is not None:
            info.tweet_id = str(r["tweet_id"])
            info.posted_at = r["posted_at"]
    return out


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


def validate_category(category: str | None) -> str | None:
    """Normalise a decision category; blank -> None; unknown -> ValueError."""
    if category is None:
        return None
    value = str(category).strip().lower()
    if not value:
        return None
    if value not in DECISION_CATEGORIES:
        raise ValueError(f"unknown category {category!r}; expected one of {DECISION_CATEGORIES}")
    return value


def _record_decision(
    conn: sqlite3.Connection,
    draft_id: int,
    action: str,
    original_text: str,
    edited_text: str | None,
    note: str | None,
    category: str | None = None,
) -> int:
    cur = conn.execute(
        """INSERT INTO decisions (draft_id, action, original_text, edited_text, note, created_at,
                                  category)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (draft_id, action, original_text, edited_text, note, _now(), category),
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
    category: str | None = None,
) -> int:
    """Save edited text over the draft and log original vs edited for voice-guide training.

    category (step 7, optional) says why: one of DECISION_CATEGORIES.
    """
    category = validate_category(category)
    row = _require(conn, draft_id)
    original = _serialise_text(row.draft.single_post, row.draft.thread)
    edited = _serialise_text(single_post, thread)
    conn.execute(
        "UPDATE drafts SET single_post = ?, thread_json = ?, updated_at = ? WHERE id = ?",
        (single_post, json.dumps(thread), _now(), draft_id),
    )
    if approve_after:
        _set_status(conn, draft_id, STATUS_APPROVED)
    did = _record_decision(conn, draft_id, ACTION_EDIT, original, edited, note, category)
    conn.commit()
    return did


def revise(
    conn: sqlite3.Connection,
    draft_id: int,
    *,
    draft: Draft,
    model: str,
    note: str | None = None,
    category: str | None = None,
) -> int:
    """Replace the draft with one the drafter rewrote on the human's instructions (note) and
    log original vs revised as a 'revise' decision. The whole Draft is replaced, claims
    included, so the caller must drop step 2b's claim checks for it. The draft stays in its
    current status: a revision is never an approval."""
    category = validate_category(category)
    row = _require(conn, draft_id)
    original = _serialise_text(row.draft.single_post, row.draft.thread)
    revised = _serialise_text(draft.single_post, draft.thread)
    conn.execute(
        """UPDATE drafts SET single_post = ?, thread_json = ?, suggested_visual = ?,
                             why_it_matters = ?, claims_json = ?, model = ?, updated_at = ?,
                             chart_json = ?, image_path = NULL, image_alt = NULL
           WHERE id = ?""",
        (
            draft.single_post,
            json.dumps(draft.thread),
            draft.suggested_visual,
            draft.why_it_matters,
            json.dumps([c.__dict__ for c in draft.claims_to_verify]),
            model,
            _now(),
            _chart_json(draft),
            draft_id,
        ),
    )
    did = _record_decision(conn, draft_id, ACTION_REVISE, original, revised, note, category)
    conn.commit()
    return did


def set_image(conn: sqlite3.Connection, draft_id: int, path: Path | None, alt: str = "") -> None:
    """Record the rendered PNG for a draft (stored relative to image_dir()) and its alt
    text, or clear both."""
    _require(conn, draft_id)
    rel = None
    if path is not None:
        try:
            rel = Path(path).resolve().relative_to(image_dir()).as_posix()
        except ValueError:
            rel = Path(path).name
    conn.execute(
        "UPDATE drafts SET image_path = ?, image_alt = ?, updated_at = ? WHERE id = ?",
        (rel, alt if rel else "", _now(), draft_id),
    )
    conn.commit()


@dataclass
class ImageGradeRow:
    id: int
    draft_id: int
    iteration: int
    score: int
    flaws: list[str]
    fixes: list[str]
    adjustments: dict
    style: dict
    model: str
    kept: bool
    created_at: str


def record_image_grade(
    conn: sqlite3.Connection,
    draft_id: int,
    *,
    iteration: int,
    score: int,
    flaws: list[str],
    fixes: list[str],
    adjustments: dict,
    style: dict,
    model: str,
) -> int:
    """One grader verdict for one render of a draft's image (draft/grader.py)."""
    _require(conn, draft_id)
    cur = conn.execute(
        "INSERT INTO image_grades (draft_id, iteration, score, flaws_json, fixes_json, "
        "adjustments_json, style_json, model, kept, created_at) VALUES (?,?,?,?,?,?,?,?,0,?)",
        (
            draft_id,
            iteration,
            score,
            json.dumps(flaws),
            json.dumps(fixes),
            json.dumps(adjustments),
            json.dumps(style),
            model,
            _now(),
        ),
    )
    conn.commit()
    return int(cur.lastrowid)


def mark_image_grade_kept(conn: sqlite3.Connection, draft_id: int, grade_id: int) -> None:
    """Flag the grade whose render the draft keeps; every other grade of the draft is not."""
    conn.execute("UPDATE image_grades SET kept = 0 WHERE draft_id = ?", (draft_id,))
    conn.execute("UPDATE image_grades SET kept = 1 WHERE id = ?", (grade_id,))
    conn.commit()


def clear_image_grades(conn: sqlite3.Connection, draft_id: int) -> None:
    conn.execute("DELETE FROM image_grades WHERE draft_id = ?", (draft_id,))
    conn.commit()


def list_image_grades(conn: sqlite3.Connection, draft_id: int) -> list[ImageGradeRow]:
    rows = conn.execute(
        "SELECT * FROM image_grades WHERE draft_id = ? ORDER BY iteration, id", (draft_id,)
    ).fetchall()
    return [
        ImageGradeRow(
            id=r["id"],
            draft_id=r["draft_id"],
            iteration=r["iteration"],
            score=r["score"],
            flaws=json.loads(r["flaws_json"] or "[]"),
            fixes=json.loads(r["fixes_json"] or "[]"),
            adjustments=json.loads(r["adjustments_json"] or "{}"),
            style=json.loads(r["style_json"] or "{}"),
            model=r["model"],
            kept=bool(r["kept"]),
            created_at=r["created_at"],
        )
        for r in rows
    ]


def drop_image(conn: sqlite3.Connection, draft_id: int, note: str | None = None) -> None:
    """The human decided the post goes out without its chart: forget the chart spec and the
    image, delete the file, and log an 'edit' decision with the text unchanged so the history
    shows it. Nothing else about the draft changes."""
    row = _require(conn, draft_id)
    path = resolve_image(row.image_path)
    conn.execute(
        "UPDATE drafts SET chart_json = NULL, image_path = NULL, image_alt = NULL, "
        "updated_at = ? WHERE id = ?",
        (_now(), draft_id),
    )
    text = _serialise_text(row.draft.single_post, row.draft.thread)
    _record_decision(conn, draft_id, ACTION_EDIT, text, text, note or "image dropped", None)
    conn.commit()
    if path is not None:
        try:
            path.unlink()
        except OSError:
            pass


def chart_for(conn: sqlite3.Connection, draft_id: int) -> Chart | None:
    r = conn.execute("SELECT chart_json FROM drafts WHERE id = ?", (draft_id,)).fetchone()
    return chart_from_json(r["chart_json"]) if r else None


def visual_for(conn: sqlite3.Connection, draft_id: int) -> Chart | Table | None:
    r = conn.execute("SELECT chart_json FROM drafts WHERE id = ?", (draft_id,)).fetchone()
    return visual_from_json(r["chart_json"]) if r else None


def drop_table(conn: sqlite3.Connection, draft_id: int, reason: str) -> None:
    """Step 2b decided the draft's table cannot be shown (a contradicted cell, too few
    supported cells). Same as drop_image but with the fact-checker's reason in the decision
    note, so the history says why the post went out text-only."""
    drop_image(conn, draft_id, note=f"table dropped by the fact-checker: {reason}")


def reject(
    conn: sqlite3.Connection,
    draft_id: int,
    note: str | None = None,
    category: str | None = None,
) -> int:
    """Mark rejected. category (step 7, optional) is one of DECISION_CATEGORIES."""
    category = validate_category(category)
    row = _require(conn, draft_id)
    _set_status(conn, draft_id, STATUS_REJECTED)
    did = _record_decision(
        conn, draft_id, ACTION_REJECT, row.draft.single_post, None, note, category
    )
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


# ---------------------------------------------------------------------------
# Step 7: voice learning loop (examples audit trail + read adapters)
# ---------------------------------------------------------------------------


def record_examples(
    conn: sqlite3.Connection,
    draft_id: int,
    edit_ids: Iterable[int] = (),
    rejection_ids: Iterable[int] = (),
) -> int:
    """Log which decisions were shown as examples when this draft was generated.

    Returns the number of rows written. This is the audit trail for "did the examples help";
    step 4's report can join draft_examples to posts later.
    """
    now = _now()
    rows = [(draft_id, int(d), EXAMPLE_KIND_EDIT, now) for d in edit_ids]
    rows += [(draft_id, int(d), EXAMPLE_KIND_REJECTION, now) for d in rejection_ids]
    if rows:
        conn.executemany(
            "INSERT INTO draft_examples (draft_id, decision_id, kind, created_at)"
            " VALUES (?, ?, ?, ?)",
            rows,
        )
        conn.commit()
    return len(rows)


def list_examples(conn: sqlite3.Connection, draft_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM draft_examples WHERE draft_id = ? ORDER BY id", (draft_id,)
    ).fetchall()


def _since_text(since: datetime | str | None) -> str:
    if since is None:
        return ""
    if isinstance(since, datetime):
        if since.tzinfo is None:
            since = since.replace(tzinfo=UTC)
        return since.astimezone(UTC).replace(microsecond=0).isoformat()
    return str(since)


def _items_table_present(conn: sqlite3.Connection) -> bool:
    row = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='items'").fetchone()
    return row is not None


def fetch_decisions_for_voice(
    conn: sqlite3.Connection, since: datetime | str | None = None
) -> list[sqlite3.Row]:
    """Decisions made at or after `since`, joined with their draft and the item's source/url.

    Columns: id, draft_id, action, original_text, edited_text, note, category, created_at,
    draft_status, draft_created_at, item_id, source, url. Oldest first. On a database without
    step 1's items table, source and url are empty strings. This (with fetch_draft_stats) is
    the ONLY place step 7 reads the items table, and only for source and url.
    """
    if _items_table_present(conn):
        item_cols = "COALESCE(i.source, '') AS source, COALESCE(i.url, '') AS url"
        item_join = "LEFT JOIN items i ON i.id = d.item_id"
    else:
        item_cols = "'' AS source, '' AS url"
        item_join = ""
    sql = f"""
        SELECT x.id, x.draft_id, x.action, x.original_text, x.edited_text, x.note,
               x.category, x.created_at,
               d.status AS draft_status, d.created_at AS draft_created_at, d.item_id,
               {item_cols}
        FROM decisions x
        JOIN drafts d ON d.id = x.draft_id
        {item_join}
        WHERE x.created_at >= ?
        ORDER BY x.created_at, x.id
    """
    return conn.execute(sql, (_since_text(since),)).fetchall()


def fetch_draft_stats(
    conn: sqlite3.Connection, since: datetime | str | None = None
) -> list[sqlite3.Row]:
    """Drafts created at or after `since`: id, item_id, source, status, created_at, model.

    source is '' when step 1's items table is absent. Oldest first.
    """
    if _items_table_present(conn):
        source_col = "COALESCE(i.source, '') AS source"
        item_join = "LEFT JOIN items i ON i.id = d.item_id"
    else:
        source_col = "'' AS source"
        item_join = ""
    sql = f"""
        SELECT d.id, d.item_id, {source_col}, d.status, d.created_at, d.model
        FROM drafts d
        {item_join}
        WHERE d.created_at >= ?
        ORDER BY d.created_at, d.id
    """
    return conn.execute(sql, (_since_text(since),)).fetchall()
