"""SQLite storage for drafts and decisions, plus the read-only adapter onto step 1's tables.

Owns four tables (created with CREATE TABLE IF NOT EXISTS in the shared pipeline DB):

  drafts(id INTEGER PK, item_id TEXT UNIQUE, cluster_id, model, thread_json,
         suggested_visual, why_it_matters, claims_json, status, rejection_reason,
         created_at, updated_at,
         chart_json, image_path, images_json, format_json)   -- added by guarded
            -- migrations: a visual spec (draft/chart.py), the pictures (relative to
            -- image_dir()) and the draft's shape
  decisions(id INTEGER PK, draft_id FK, action, original_text, edited_text, note, created_at,
            category)
            -- action 'revise': the studio rewrote the piece on the editor's request (note);
            -- original_text/edited_text hold the before/after like an 'edit'.
  draft_examples, image_grades   -- left from the retired drafter: still created so an old
            -- database opens unchanged, never read or written

Never modifies the items or scores tables. items.cluster_id is read to follow a story that
linking merged (drafted_cluster_ids). The studio's studio_pieces is read only by
studio_hold (read-only, nothing when the table is missing).
"""

from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from draft.chart import (
    IMAGES_DIRNAME,
    Chart,
    chart_from_json,
    table_from_json,
)
from draft.schema import Claim, Draft

DEFAULT_DB_PATH = "./pipeline.db"

STATUS_PENDING = "pending"
STATUS_APPROVED = "approved"
STATUS_REJECTED = "rejected"
STATUS_FAILED = "failed"  # drafter produced output that broke a hard rule
STATUSES = (STATUS_PENDING, STATUS_APPROVED, STATUS_REJECTED, STATUS_FAILED)
# Statuses a reviewer may still change a draft in: only a draft still awaiting a decision.
EDITABLE_STATUSES = (STATUS_PENDING,)

ACTION_APPROVE = "approve"
ACTION_EDIT = "edit"
ACTION_REJECT = "reject"
ACTION_REOPEN = "reopen"  # an approved draft was moved back to pending
ACTION_REVISE = "revise"  # the drafter rewrote the draft on the human's instructions
ACTIONS = (ACTION_APPROVE, ACTION_EDIT, ACTION_REJECT, ACTION_REOPEN, ACTION_REVISE)

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

_SCHEMA = """
CREATE TABLE IF NOT EXISTS drafts (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id          TEXT NOT NULL UNIQUE,
    cluster_id       INTEGER,
    model            TEXT NOT NULL,
    thread_json      TEXT NOT NULL,
    suggested_visual TEXT NOT NULL DEFAULT '',
    why_it_matters   TEXT NOT NULL DEFAULT '',
    claims_json      TEXT NOT NULL DEFAULT '[]',
    status           TEXT NOT NULL DEFAULT 'pending',
    rejection_reason TEXT,
    snoozed_until    TEXT,   -- unused: the snooze feature is gone. Kept so that old
                             -- databases need no table rebuild; never read or written.
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_drafts_status ON drafts(status);
CREATE INDEX IF NOT EXISTS idx_drafts_cluster ON drafts(cluster_id);
CREATE INDEX IF NOT EXISTS idx_drafts_created ON drafts(created_at);

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
    (
        "image_grades",
        "criteria_json",
        "ALTER TABLE image_grades ADD COLUMN criteria_json TEXT NOT NULL DEFAULT '{}'",
    ),
    # Step 9 phase four: the draft's shape, its extra visuals and every rendered picture.
    ("drafts", "format_json", "ALTER TABLE drafts ADD COLUMN format_json TEXT"),
    ("drafts", "images_json", "ALTER TABLE drafts ADD COLUMN images_json TEXT"),
    # Step 9 phase three: the Style the draft's pictures start from (its designer's), so a
    # redraw after a revision or by the verifier keeps it instead of the house style.
    ("drafts", "style_json", "ALTER TABLE drafts ADD COLUMN style_json TEXT"),
    # Step 9 human jury: the other variant while a draft awaits the A/B pick.
    ("drafts", "choice_json", "ALTER TABLE drafts ADD COLUMN choice_json TEXT"),
)


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def _migrate(conn: sqlite3.Connection) -> None:
    for table, column, ddl in _MIGRATIONS:
        if column not in _columns(conn, table):
            conn.execute(ddl)
    # Drafts are threads only: a database from before that carries the NOT NULL single_post
    # column, which every insert would have to fill. Drop it (SQLite >= 3.35). Its text is
    # not lost to the history: every decision row stored it in original_text.
    if "single_post" in _columns(conn, "drafts"):
        conn.execute("ALTER TABLE drafts DROP COLUMN single_post")
    # The snooze feature is gone: a draft left snoozed in an older database becomes pending
    # again, so it is reviewed rather than hidden forever. Idempotent, and a no-op on a
    # fresh DB (no such row).
    conn.execute("UPDATE drafts SET status = ? WHERE status = 'snoozed'", (STATUS_PENDING,))
    # The retired swarm's A/B pick is gone too: a draft left awaiting it holds one variant, which
    # becomes an ordinary pending draft.
    conn.execute("UPDATE drafts SET status = ? WHERE status = 'choosing'", (STATUS_PENDING,))
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


def image_file(draft_id: int, index: int = 0) -> Path:
    """The PNG for a draft's visual: draft_<id>.png for the first, draft_<id>_<k>.png after."""
    suffix = f"_{index + 1}" if index else ""
    return image_dir() / f"draft_{draft_id}{suffix}.png"


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


def _format_json(draft: Draft) -> str:
    """drafts.format_json: the shape, anchors, limit and wanted visual count (phase four)
    plus any extra charts beyond the first visual."""
    d = draft.format_dict()
    d["extra_visuals"] = [c.to_dict() for c in draft.extra_visuals]
    return json.dumps(d, ensure_ascii=False)


def _apply_format_json(draft: Draft, text: str | None) -> None:
    if not text:
        return
    try:
        d = json.loads(text)
    except ValueError:
        return
    if not isinstance(d, dict):
        return
    draft.apply_format_dict(d)
    extras = []
    for raw in d.get("extra_visuals") or []:
        c = chart_from_json(json.dumps(raw)) if isinstance(raw, dict) else None
        if c is not None:
            extras.append(c)
    draft.extra_visuals = extras


def _images_list(text: str | None) -> list[dict]:
    """drafts.images_json parsed: [{index, path, alt, anchor}] sorted by index."""
    if not text:
        return []
    try:
        data = json.loads(text)
    except ValueError:
        return []
    if not isinstance(data, list):
        return []
    out = [d for d in data if isinstance(d, dict) and d.get("path")]
    return sorted(out, key=lambda d: int(d.get("index", 0)))


def connect(path: str | Path | None = None) -> sqlite3.Connection:
    """Open the shared pipeline DB and make sure our tables exist."""
    # check_same_thread=False: FastAPI opens the connection in a worker thread and uses it
    # on the event loop (or in the threadpool a slow route hands its work to); each request
    # uses its connection sequentially, so this is safe. timeout=30: a CLI run (run_studio,
    # run_publish) writing the same file makes us wait, not fail, as the other stores do.
    conn = sqlite3.connect(str(path or db_path()), check_same_thread=False, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(_SCHEMA)
    _migrate(conn)
    return conn


# ---------------------------------------------------------------------------
# Adapter onto step 1's tables
# ---------------------------------------------------------------------------


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
    # Phase four: every rendered picture, [{index, path, alt, anchor}]; the first is the
    # same picture as image_path.
    images: list[dict] = field(default_factory=list)

    @property
    def editable(self) -> bool:
        """Whether the reviewer may still change this draft (it awaits a decision)."""
        return self.status in EDITABLE_STATUSES

    @property
    def studio_piece(self) -> int | None:
        """The studio piece this draft came from (None: the drafter wrote it). A studio
        draft is revised in the studio, which resumes the session that wrote it."""
        return studio_piece_id(self.item_id)


def _row_to_draft(r: sqlite3.Row) -> DraftRow:
    keys = r.keys()
    draft = Draft(
        thread=json.loads(r["thread_json"]),
        suggested_visual=r["suggested_visual"],
        why_it_matters=r["why_it_matters"],
        claims_to_verify=[Claim(**c) for c in json.loads(r["claims_json"])],
        chart=chart_from_json(r["chart_json"]) if "chart_json" in keys else None,
        table=table_from_json(r["chart_json"]) if "chart_json" in keys else None,
    )
    if "format_json" in keys:
        _apply_format_json(draft, r["format_json"])
    return DraftRow(
        id=r["id"],
        item_id=r["item_id"],
        cluster_id=r["cluster_id"],
        model=r["model"],
        draft=draft,
        status=r["status"],
        rejection_reason=r["rejection_reason"],
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
        images=_images_list(r["images_json"]) if "images_json" in keys else [],
    )


STUDIO_ITEM_PREFIX = "studio:"


def studio_item_id(piece_id: int) -> str:
    """The item_id of a draft the studio (studio/) wrote: one long session per post, with
    its own fact base and cards. Such a draft is revised in the studio, not by the drafter."""
    return f"{STUDIO_ITEM_PREFIX}{int(piece_id)}"


def studio_piece_id(item_id: str | None) -> int | None:
    """The studio piece behind a draft, or None for a drafter draft."""
    if not item_id or not item_id.startswith(STUDIO_ITEM_PREFIX):
        return None
    tail = item_id[len(STUDIO_ITEM_PREFIX) :]
    return int(tail) if tail.isdigit() else None


#: A studio draft's why_it_matters carries each fast-moving fact to re-check on posting day
#: as a line of its own starting with this (studio/ingest.py writes them); the copy-paste
#: posting page lists them above the posts.
RECHECK_PREFIX = "Re-check before posting: "


def recheck_lines(why_it_matters: str | None) -> list[str]:
    """The re-check lines of a draft's why_it_matters (pure); empty for a drafter draft."""
    return [
        line[len(RECHECK_PREFIX) :].strip()
        for line in (why_it_matters or "").splitlines()
        if line.startswith(RECHECK_PREFIX) and line[len(RECHECK_PREFIX) :].strip()
    ]


def studio_hold(conn: sqlite3.Connection, row: DraftRow) -> str:
    """Why the queue must leave a studio draft alone right now, or "" when it may act on it:
    the studio is working on the piece (a running stage, a revision most often), or the
    editor asked for a studio run that has not started yet. When that run lands it replaces
    the draft's text and cards, so an approval, an edit, a dropped picture or a rejection
    made meanwhile would be overwritten or make the revision fail after an hour's work.
    A read-only look at the studio's own studio_pieces (studio/store.py owns it); "" for a
    drafter draft, or before the studio's first run when the table does not exist."""
    piece_id = row.studio_piece
    if piece_id is None:
        return ""
    from studio import store as studio_store

    present = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'studio_pieces'"
    ).fetchone()
    if present is None:
        return ""
    r = conn.execute(
        "SELECT stage, request FROM studio_pieces WHERE id = ?", (piece_id,)
    ).fetchone()
    if r is None:
        return ""
    if r["stage"] in studio_store.RUNNING_STAGES:
        return (
            f"the studio is working on piece {piece_id} right now ({r['stage']}); when it "
            "finishes it replaces this draft's text and cards. Wait for it, or stop the run on "
            "the runs page first (a piece whose run was stopped is let go as soon as the panel "
            f"sees the run end, or its page /studio/{piece_id} is opened)"
        )
    if r["request"]:
        return (
            f"piece {piece_id} has a studio run waiting ({r['request']} asked for on its "
            "studio page); it replaces this draft's text and cards when it lands. Wait for it"
        )
    return ""


def find_by_item(conn: sqlite3.Connection, item_id: str) -> DraftRow | None:
    """The draft written for this item_id, if there is one (item_id is unique)."""
    r = conn.execute("SELECT id FROM drafts WHERE item_id = ?", (item_id,)).fetchone()
    return get_draft(conn, int(r[0])) if r else None


def drafted_cluster_ids(conn: sqlite3.Connection) -> set[int]:
    """Every story with a draft that did not fail the hard rules, whatever else its status
    (waiting, approved, rejected, posted): the studio's shortlist leaves these out
    (studio/runner.py:taken_stories). One story, one piece of writing. A draft's story is also
    followed through its item to the cluster that holds it now, since story linking folds
    one cluster into another."""
    ids = {
        int(r[0])
        for r in conn.execute(
            "SELECT cluster_id FROM drafts WHERE cluster_id IS NOT NULL AND status != ?",
            (STATUS_FAILED,),
        )
    }
    if step1_tables_present(conn):
        ids |= {
            int(r[0])
            for r in conn.execute(
                "SELECT i.cluster_id FROM drafts d JOIN items i ON i.id = d.item_id "
                "WHERE d.status != ? AND i.cluster_id IS NOT NULL",
                (STATUS_FAILED,),
            )
        }
    return ids


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
        """INSERT INTO drafts (item_id, cluster_id, model, thread_json,
                               suggested_visual, why_it_matters, claims_json, status,
                               rejection_reason, created_at, updated_at, chart_json,
                               format_json)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            item_id,
            cluster_id,
            model,
            json.dumps(draft.thread),
            draft.suggested_visual,
            draft.why_it_matters,
            json.dumps([c.__dict__ for c in draft.claims_to_verify]),
            status,
            rejection_reason,
            now,
            now,
            _chart_json(draft),
            _format_json(draft),
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


#: Schedule states a draft may be reopened from: the only ones that mean nothing of it is
#: on X and no publish run owns it. Posted, partial and claimed hold it, as does anything
#: unrecognised.
REOPENABLE_STATES = ("pending", "failed", "refused")


@dataclass
class PublishInfo:
    """What step 3 did with a draft, read from its schedule/posts tables (never written here)."""

    status: str  # posted | partial | failed | refused | claimed | pending
    tweet_id: str | None = None  # first post's tweet id, when it is live
    posted_at: str | None = None
    error: str | None = None
    position: int | None = None  # the human's publishing order (panel), 1 = next to post
    claimed_at: str | None = None  # when a publish run took the draft (UTC), for a stale claim

    @property
    def tweet_url(self) -> str:
        if not self.tweet_id or not self.tweet_id.isdigit():
            return ""  # a hand-posted thread without its URL has no X id to link
        return f"https://x.com/i/web/status/{self.tweet_id}"

    @property
    def reopenable(self) -> bool:
        """Whether a human may still take this draft back. An allowlist, so a state a later
        step 3 invents holds the draft instead of falling through. Both the button's
        visibility and the route's refusal read this one property, so they cannot drift."""
        return self.status in REOPENABLE_STATES


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
    cols = {r[1] for r in conn.execute("PRAGMA table_info(schedule)").fetchall()}
    position = "position" if "position" in cols else "NULL AS position"
    out: dict[int, PublishInfo] = {}
    for r in conn.execute(
        f"SELECT draft_id, status, error, claimed_at, {position} FROM schedule"
        f" WHERE draft_id IN ({marks})",
        draft_ids,
    ).fetchall():
        out[int(r["draft_id"])] = PublishInfo(
            status=str(r["status"]),
            error=r["error"],
            claimed_at=r["claimed_at"],
            position=int(r["position"]) if r["position"] is not None else None,
        )
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
    """Drafts in a status, newest first."""
    where = " WHERE d.status = ?"
    params: tuple[Any, ...] = (status,)
    order = " ORDER BY d.created_at DESC, d.id DESC"
    rows = conn.execute(_select_drafts_sql(conn) + where + order, params).fetchall()
    return [_row_to_draft(r) for r in rows]


def _set_status(conn: sqlite3.Connection, draft_id: int, status: str) -> None:
    conn.execute(
        "UPDATE drafts SET status = ?, updated_at = ? WHERE id = ?",
        (status, _now(), draft_id),
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
    did = _record_decision(
        conn, draft_id, ACTION_APPROVE, _serialise_text(row.draft.thread), None, note
    )
    conn.commit()
    return did


def edit(
    conn: sqlite3.Connection,
    draft_id: int,
    *,
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
    original = _serialise_text(row.draft.thread)
    edited = _serialise_text(thread)
    conn.execute(
        "UPDATE drafts SET thread_json = ?, updated_at = ? WHERE id = ?",
        (json.dumps(thread), _now(), draft_id),
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
    original = _serialise_text(row.draft.thread)
    revised = _serialise_text(draft.thread)
    conn.execute(
        """UPDATE drafts SET thread_json = ?, suggested_visual = ?,
                             why_it_matters = ?, claims_json = ?, model = ?, updated_at = ?,
                             chart_json = ?, image_path = NULL, image_alt = NULL,
                             format_json = ?, images_json = NULL
           WHERE id = ?""",
        (
            json.dumps(draft.thread),
            draft.suggested_visual,
            draft.why_it_matters,
            json.dumps([c.__dict__ for c in draft.claims_to_verify]),
            model,
            _now(),
            _chart_json(draft),
            _format_json(draft),
            draft_id,
        ),
    )
    did = _record_decision(conn, draft_id, ACTION_REVISE, original, revised, note, category)
    conn.commit()
    return did


def set_image(
    conn: sqlite3.Connection,
    draft_id: int,
    path: Path | None,
    alt: str = "",
    index: int = 0,
) -> None:
    """Record the rendered PNG for a draft's visual `index` (stored relative to image_dir())
    and its alt text, or clear it. Index 0 also keeps drafts.image_path / image_alt (the
    first picture, what every pre-phase-four reader uses); every picture goes into
    drafts.images_json with the post it is anchored to."""
    row = _require(conn, draft_id)
    rel = None
    if path is not None:
        try:
            rel = Path(path).resolve().relative_to(image_dir()).as_posix()
        except ValueError:
            rel = Path(path).name
    anchors = row.draft.anchors or [1]
    anchor = anchors[index] if index < len(anchors) else 1
    images = [d for d in row.images if int(d.get("index", 0)) != index]
    if rel:
        images.append({"index": index, "path": rel, "alt": alt, "anchor": anchor})
    images.sort(key=lambda d: int(d.get("index", 0)))
    if index == 0:
        conn.execute(
            "UPDATE drafts SET image_path = ?, image_alt = ?, images_json = ?, updated_at = ? "
            "WHERE id = ?",
            (rel, alt if rel else "", json.dumps(images), _now(), draft_id),
        )
    else:
        conn.execute(
            "UPDATE drafts SET images_json = ?, updated_at = ? WHERE id = ?",
            (json.dumps(images), _now(), draft_id),
        )
    conn.commit()


def drop_image(
    conn: sqlite3.Connection,
    draft_id: int,
    note: str | None = None,
    index: int | None = None,
) -> None:
    """The human decided the post goes out without its chart: forget the chart spec and the
    image, delete the file, and log an 'edit' decision with the text unchanged so the history
    shows it. Nothing else about the draft changes.

    With `index` only that one picture goes (phase four: a draft may carry two). The ones
    after it move down a place, so index 0 stays the picture drafts.image_path names and
    drafts.images_json stays 0-based with no gap; a draft left with no picture ends up
    exactly where dropping them all would leave it. Raises IndexError when the draft has
    no picture at that index."""
    row = _require(conn, draft_id)
    if index is not None:
        _drop_one_image(conn, row, index, note)
        return
    paths = [resolve_image(row.image_path)] + [resolve_image(d["path"]) for d in row.images]
    draft = row.draft
    draft.extra_visuals = []
    conn.execute(
        "UPDATE drafts SET chart_json = NULL, image_path = NULL, image_alt = NULL, "
        "images_json = NULL, format_json = ?, updated_at = ? WHERE id = ?",
        (_format_json(draft), _now(), draft_id),
    )
    text = _serialise_text(row.draft.thread)
    _record_decision(conn, draft_id, ACTION_EDIT, text, text, note or "image dropped", None)
    conn.commit()
    for path in {p for p in paths if p is not None}:
        try:
            path.unlink()
        except OSError:
            pass


def _drop_one_image(conn: sqlite3.Connection, row: DraftRow, index: int, note: str | None) -> None:
    """One picture of a draft goes; the others stay (see drop_image)."""
    draft = row.draft
    entry = next((d for d in row.images if int(d.get("index", 0)) == index), None)
    if entry is None and index >= len(draft.visuals):
        raise IndexError(f"draft {row.id} has no picture at index {index}")
    gone = [resolve_image(entry["path"])] if entry else []
    if index == 0:
        gone.append(resolve_image(row.image_path))
    # The spec that made this picture goes with it: index 0 is chart_json (a chart or a
    # table), anything after it is the extra chart at that place.
    if index == 0:
        promoted = draft.extra_visuals.pop(0) if draft.extra_visuals else None
        draft.chart = promoted if isinstance(promoted, Chart) else None
        draft.table = None
    elif index - 1 < len(draft.extra_visuals):
        draft.extra_visuals.pop(index - 1)
    if index < len(draft.anchors) and len(draft.anchors) > 1:
        draft.anchors = draft.anchors[:index] + draft.anchors[index + 1 :]
    images = [
        dict(d, index=int(d.get("index", 0)) - 1) if int(d.get("index", 0)) > index else dict(d)
        for d in row.images
        if int(d.get("index", 0)) != index
    ]
    images.sort(key=lambda d: int(d.get("index", 0)))
    first = images[0] if images and int(images[0].get("index", 0)) == 0 else None
    conn.execute(
        "UPDATE drafts SET chart_json = ?, image_path = ?, image_alt = ?, images_json = ?, "
        "format_json = ?, updated_at = ? WHERE id = ?",
        (
            _chart_json(draft),
            first["path"] if first else None,
            (first.get("alt") or "") if first else "",
            json.dumps(images) if images else None,
            _format_json(draft),
            _now(),
            row.id,
        ),
    )
    text = _serialise_text(draft.thread)
    _record_decision(
        conn, row.id, ACTION_EDIT, text, text, note or f"image {index + 1} dropped", None
    )
    conn.commit()
    keep = {resolve_image(d["path"]) for d in images}
    for path in {p for p in gone if p is not None} - keep:
        try:
            path.unlink()
        except OSError:
            pass


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
        conn, draft_id, ACTION_REJECT, _serialise_text(row.draft.thread), None, note, category
    )
    conn.commit()
    return did


def reopen(conn: sqlite3.Connection, draft_id: int, note: str | None = None) -> int:
    """Move an approved draft back to pending, so a reviewer may edit or reject it again.
    Nothing is un-posted here: this only changes the draft's status and logs the decision.
    The caller refuses a draft that is posted or already claimed by step 3."""
    row = _require(conn, draft_id)
    _set_status(conn, draft_id, STATUS_PENDING)
    did = _record_decision(
        conn, draft_id, ACTION_REOPEN, _serialise_text(row.draft.thread), None, note
    )
    conn.commit()
    return did


def list_decisions(conn: sqlite3.Connection, draft_id: int | None = None) -> list[sqlite3.Row]:
    if draft_id is None:
        return conn.execute("SELECT * FROM decisions ORDER BY id").fetchall()
    return conn.execute(
        "SELECT * FROM decisions WHERE draft_id = ? ORDER BY id", (draft_id,)
    ).fetchall()


def _serialise_text(thread: list[str]) -> str:
    """Canonical text form of a draft for the decisions log: JSON so it round-trips.
    (Rows from before threads-only carry {"single_post", "thread"} or a bare post;
    parse_decision_text reads every form.)"""
    return json.dumps({"thread": list(thread)}, ensure_ascii=False)


def parse_decision_text(text: str | None) -> list[str]:
    """Inverse of _serialise_text: the thread a decision row stored.

    Current rows store JSON {"thread": [...]}. Rows from before threads-only stored
    {"single_post", "thread"} (the single post leads, then the thread) or a bare post; both
    still parse, so old history still reads.
    """
    if text is None:
        return []
    stripped = text.strip()
    if stripped.startswith("{"):
        try:
            data = json.loads(stripped)
        except json.JSONDecodeError:
            data = None
        if isinstance(data, dict) and ("thread" in data or "single_post" in data):
            thread = data.get("thread")
            posts = [str(p) for p in thread] if isinstance(thread, list) else []
            single = data.get("single_post")
            if single:
                posts.insert(0, str(single))
            return posts
    return [text] if text else []


#: Every studio draft's item_id, as a LIKE pattern (the prefix holds no wildcard).
_STUDIO_LIKE = STUDIO_ITEM_PREFIX + "%"
