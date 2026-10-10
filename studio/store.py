"""The studio's tables: studio_pieces (one per piece, with its stage and session),
studio_runs (one per CLI invocation), studio_topics (topics a human queued),
studio_playbook_versions (every playbook the sessions have read, and proposals) and
studio_manual_metrics (a post's numbers the editor typed in from X).

The studio owns these five tables and nothing else. It reads step 1 only through
studio/topics.py and writes into the approval queue only through studio/ingest.py,
which uses approval_queue/store.py's own functions. Its other reads of other steps'
tables are the read-only adapters at the end of this module (`fetch_posted_heads`,
`fetch_studio_edits`, `fetch_studio_reviews`), which return nothing when a table is
missing.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

# Stages of a piece. A piece moves forward through these; `failed` and `interrupted`
# can be resumed from the studio page, which sets a request and starts the studio.
STAGE_RESEARCHING = "researching"
STAGE_RESEARCH_READY = "research_ready"  # the checkpoint: a human reads the fact base
STAGE_WRITING = "writing"
STAGE_POLISHING = "polishing"
STAGE_REVISING = "revising"
STAGE_READY = "ready"  # in the approval queue as a draft
STAGE_FAILED = "failed"
STAGE_INTERRUPTED = "interrupted"  # its process died mid-stage (killed, crashed, timed out)
STAGE_DISCARDED = "discarded"
STAGES = (
    STAGE_RESEARCHING,
    STAGE_RESEARCH_READY,
    STAGE_WRITING,
    STAGE_POLISHING,
    STAGE_REVISING,
    STAGE_READY,
    STAGE_FAILED,
    STAGE_INTERRUPTED,
    STAGE_DISCARDED,
)
RUNNING_STAGES = (STAGE_RESEARCHING, STAGE_WRITING, STAGE_POLISHING, STAGE_REVISING)

# What a human asked for, picked up by the next studio run (the studio_resume step).
REQUEST_CONTINUE = "continue"  # write the piece after the research checkpoint, or retry
REQUEST_REVISE = "revise"  # rewrite a finished piece with the human's notes
REQUESTS = (REQUEST_CONTINUE, REQUEST_REVISE)

ORIGIN_AUTO = "auto"
ORIGIN_MANUAL = "manual"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS studio_pieces (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    origin          TEXT NOT NULL,
    topic           TEXT NOT NULL DEFAULT '',
    cluster_id      INTEGER,
    story_item      TEXT NOT NULL DEFAULT '',
    requested_angle TEXT NOT NULL DEFAULT '',
    angle           TEXT NOT NULL DEFAULT '',
    shape           TEXT NOT NULL DEFAULT '',
    hook_style      TEXT NOT NULL DEFAULT '',
    title           TEXT NOT NULL DEFAULT '',
    stage           TEXT NOT NULL,
    checkpoint      INTEGER NOT NULL DEFAULT 0,
    session_id      TEXT NOT NULL,
    workspace       TEXT NOT NULL,
    model           TEXT NOT NULL,
    effort          TEXT NOT NULL DEFAULT '',
    draft_id        INTEGER,
    request         TEXT NOT NULL DEFAULT '',
    request_note    TEXT NOT NULL DEFAULT '',
    error           TEXT NOT NULL DEFAULT '',
    meta_json       TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_studio_pieces_stage ON studio_pieces(stage);
CREATE INDEX IF NOT EXISTS idx_studio_pieces_created ON studio_pieces(created_at);

CREATE TABLE IF NOT EXISTS studio_runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    piece_id    INTEGER NOT NULL REFERENCES studio_pieces(id),
    stage       TEXT NOT NULL,
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    outcome     TEXT NOT NULL DEFAULT '',
    detail      TEXT NOT NULL DEFAULT '',
    turns       INTEGER NOT NULL DEFAULT 0,
    cost_usd    REAL NOT NULL DEFAULT 0,
    duration_ms INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_studio_runs_piece ON studio_runs(piece_id);

CREATE TABLE IF NOT EXISTS studio_topics (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    topic      TEXT NOT NULL DEFAULT '',
    cluster_id INTEGER,
    story_item TEXT NOT NULL DEFAULT '',
    angle      TEXT NOT NULL DEFAULT '',
    checkpoint INTEGER NOT NULL DEFAULT 1,
    piece_id   INTEGER
);

CREATE TABLE IF NOT EXISTS studio_scans (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    status      TEXT NOT NULL DEFAULT 'running',
    model       TEXT NOT NULL DEFAULT '',
    effort      TEXT NOT NULL DEFAULT '',
    summary     TEXT NOT NULL DEFAULT '',
    topics      INTEGER NOT NULL DEFAULT 0,
    catalysts   INTEGER NOT NULL DEFAULT 0,
    detail      TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS studio_radar_topics (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    scan_id     INTEGER NOT NULL,
    created_at  TEXT NOT NULL,
    rank        INTEGER NOT NULL DEFAULT 0,
    title       TEXT NOT NULL,
    why_now     TEXT NOT NULL DEFAULT '',
    angle       TEXT NOT NULL DEFAULT '',
    companies   TEXT NOT NULL DEFAULT '[]',
    sources     TEXT NOT NULL DEFAULT '[]',
    status      TEXT NOT NULL DEFAULT 'new',
    topic_id    INTEGER,
    piece_id    INTEGER
);

CREATE TABLE IF NOT EXISTS studio_catalysts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    key         TEXT NOT NULL UNIQUE,
    date_start  TEXT NOT NULL,
    date_end    TEXT NOT NULL,
    when_text   TEXT NOT NULL,
    company     TEXT NOT NULL,
    ticker      TEXT NOT NULL DEFAULT '',
    drug        TEXT NOT NULL DEFAULT '',
    kind        TEXT NOT NULL DEFAULT 'other',
    detail      TEXT NOT NULL DEFAULT '',
    source      TEXT NOT NULL DEFAULT '',
    origin      TEXT NOT NULL DEFAULT '',
    first_seen  TEXT NOT NULL,
    last_seen   TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'open',
    topic_id    INTEGER,
    piece_id    INTEGER
);
CREATE INDEX IF NOT EXISTS idx_studio_catalysts_start ON studio_catalysts(date_start);

CREATE TABLE IF NOT EXISTS studio_playbook_versions (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    source     TEXT NOT NULL,
    text       TEXT NOT NULL,
    changelog  TEXT NOT NULL DEFAULT '[]',
    evidence   TEXT NOT NULL DEFAULT '',
    applied    INTEGER NOT NULL DEFAULT 1,
    based_on   INTEGER,
    pieces     TEXT NOT NULL DEFAULT '[]'  -- the scored pieces a learned version was given
);

CREATE TABLE IF NOT EXISTS studio_manual_metrics (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    piece_id    INTEGER NOT NULL,
    captured_at TEXT NOT NULL,
    impressions INTEGER NOT NULL DEFAULT 0,
    likes       INTEGER NOT NULL DEFAULT 0,
    reposts     INTEGER NOT NULL DEFAULT 0,
    replies     INTEGER NOT NULL DEFAULT 0,
    quotes      INTEGER NOT NULL DEFAULT 0,
    bookmarks   INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_studio_manual_metrics_piece ON studio_manual_metrics(piece_id);
"""

# Where a playbook version came from. `proposal` is a learned rewrite kept for the editor
# (learn.playbook: propose) and not read by sessions until applied; every other source
# is applied when it is written.
PLAYBOOK_SEED = "seed"
PLAYBOOK_EDITOR = "editor"
PLAYBOOK_LEARNED = "learned"
PLAYBOOK_PROPOSAL = "proposal"
PLAYBOOK_REVERT = "revert"
PLAYBOOK_SOURCES = (
    PLAYBOOK_SEED,
    PLAYBOOK_EDITOR,
    PLAYBOOK_LEARNED,
    PLAYBOOK_PROPOSAL,
    PLAYBOOK_REVERT,
)
MANUAL_METRICS = ("impressions", "likes", "reposts", "replies", "quotes", "bookmarks")

# Guarded migrations: columns added after the tables first shipped.
# story_item: one item (step 1's items.id) of the feed story beside cluster_id. Story
# linking (filter/link.py) folds a cluster into another and deletes it, and the items move
# with it, so the item finds the story again (runner.follow_merges).
# voice: the key of the playbook voice the piece is written in (studio/voices.py), '' for
# a piece from before voices or with voices off.
_MIGRATIONS = (
    ("studio_pieces", "story_item", "TEXT NOT NULL DEFAULT ''"),
    ("studio_topics", "story_item", "TEXT NOT NULL DEFAULT ''"),
    ("studio_pieces", "voice", "TEXT NOT NULL DEFAULT ''"),
)


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def ensure_tables(conn: sqlite3.Connection) -> None:
    conn.executescript(_SCHEMA)
    for table, column, decl in _MIGRATIONS:
        have = {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        if column not in have:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
    conn.commit()


def connect(path: str | Path) -> sqlite3.Connection:
    # check_same_thread=False: the panel opens the connection in one worker thread and may
    # use it in another (as approval_queue/store.py:connect explains).
    conn = sqlite3.connect(str(path), check_same_thread=False, timeout=30)
    conn.row_factory = sqlite3.Row
    ensure_tables(conn)
    return conn


@dataclass
class Piece:
    id: int
    created_at: str
    updated_at: str
    origin: str
    topic: str
    cluster_id: int | None
    requested_angle: str
    angle: str
    shape: str
    hook_style: str
    title: str
    stage: str
    checkpoint: bool
    session_id: str
    workspace: str
    model: str
    effort: str
    draft_id: int | None
    request: str
    request_note: str
    error: str
    story_item: str = ""  # one item of the story at cluster_id ('' when none is known)
    voice: str = ""  # the playbook voice it is written in ('' when it has none)
    meta: dict[str, Any] = field(default_factory=dict)

    @property
    def label(self) -> str:
        """What lists show: the title, else the topic's first line (a topic queued from the
        radar carries its reasons and sources on the lines below)."""
        first = self.topic.strip().splitlines()[0] if self.topic.strip() else ""
        return self.title or first or f"piece {self.id}"


def _row(r: sqlite3.Row) -> Piece:
    try:
        meta = json.loads(r["meta_json"] or "{}")
    except ValueError:
        meta = {}
    return Piece(
        id=int(r["id"]),
        created_at=r["created_at"],
        updated_at=r["updated_at"],
        origin=r["origin"],
        topic=r["topic"],
        cluster_id=r["cluster_id"],
        requested_angle=r["requested_angle"],
        angle=r["angle"],
        shape=r["shape"],
        hook_style=r["hook_style"],
        title=r["title"],
        stage=r["stage"],
        checkpoint=bool(r["checkpoint"]),
        session_id=r["session_id"],
        workspace=r["workspace"],
        model=r["model"],
        effort=r["effort"],
        draft_id=r["draft_id"],
        request=r["request"],
        request_note=r["request_note"],
        error=r["error"],
        story_item=r["story_item"] or "",
        voice=r["voice"] or "",
        meta=meta if isinstance(meta, dict) else {},
    )


def create_piece(
    conn: sqlite3.Connection,
    *,
    origin: str,
    topic: str,
    cluster_id: int | None,
    requested_angle: str,
    checkpoint: bool,
    session_id: str,
    workspace: str,
    model: str,
    effort: str,
    story_item: str = "",
) -> int:
    now = _now()
    cur = conn.execute(
        """INSERT INTO studio_pieces (created_at, updated_at, origin, topic, cluster_id,
               story_item, requested_angle, stage, checkpoint, session_id, workspace, model,
               effort)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            now,
            now,
            origin,
            topic,
            cluster_id,
            story_item,
            requested_angle,
            STAGE_RESEARCHING,
            int(checkpoint),
            session_id,
            workspace,
            model,
            effort,
        ),
    )
    conn.commit()
    return int(cur.lastrowid)


def get_piece(conn: sqlite3.Connection, piece_id: int) -> Piece | None:
    r = conn.execute("SELECT * FROM studio_pieces WHERE id = ?", (piece_id,)).fetchone()
    return _row(r) if r else None


def list_pieces(conn: sqlite3.Connection, limit: int = 50) -> list[Piece]:
    rows = conn.execute(
        "SELECT * FROM studio_pieces ORDER BY id DESC LIMIT ?", (int(limit),)
    ).fetchall()
    return [_row(r) for r in rows]


_UPDATABLE = {
    "stage",
    "angle",
    "shape",
    "hook_style",
    "title",
    "draft_id",
    "request",
    "request_note",
    "error",
    "topic",
    "cluster_id",
    "story_item",
    "session_id",
    "voice",
}


def update_piece(conn: sqlite3.Connection, piece_id: int, **fields: Any) -> None:
    meta = fields.pop("meta", None)
    bad = set(fields) - _UPDATABLE
    if bad:
        raise ValueError(f"cannot update {sorted(bad)}")
    if fields.get("stage") is not None and fields["stage"] not in STAGES:
        raise ValueError(f"unknown stage {fields['stage']!r}")
    sets = [f"{k} = ?" for k in fields]
    values: list[Any] = list(fields.values())
    if meta is not None:
        current = get_piece(conn, piece_id)
        merged = {**(current.meta if current else {}), **meta}
        sets.append("meta_json = ?")
        values.append(json.dumps(merged, ensure_ascii=False))
    sets.append("updated_at = ?")
    values.append(_now())
    conn.execute(f"UPDATE studio_pieces SET {', '.join(sets)} WHERE id = ?", (*values, piece_id))
    conn.commit()


def request(conn: sqlite3.Connection, piece_id: int, what: str, note: str = "") -> None:
    """Record a human's request for the next studio run."""
    if what not in REQUESTS:
        raise ValueError(f"unknown request {what!r}")
    update_piece(conn, piece_id, request=what, request_note=note.strip())


def pending_requests(conn: sqlite3.Connection) -> list[Piece]:
    rows = conn.execute(
        "SELECT * FROM studio_pieces WHERE request != '' ORDER BY updated_at, id"
    ).fetchall()
    return [_row(r) for r in rows]


def running_pieces(conn: sqlite3.Connection) -> list[Piece]:
    marks = ",".join("?" for _ in RUNNING_STAGES)
    rows = conn.execute(
        f"SELECT * FROM studio_pieces WHERE stage IN ({marks}) ORDER BY id", RUNNING_STAGES
    ).fetchall()
    return [_row(r) for r in rows]


def offered_elsewhere(conn: sqlite3.Connection, piece_id: int) -> set[int]:
    """The stories offered to the other pieces still researching on no story of their own
    (their `offered_stories`): a piece written beside them is not offered the same ones."""
    out: set[int] = set()
    for p in running_pieces(conn):
        if p.id == piece_id or p.stage != STAGE_RESEARCHING or p.cluster_id is not None:
            continue
        for sid in p.meta.get("offered_stories") or []:
            if isinstance(sid, int) or (isinstance(sid, str) and sid.isdigit()):
                out.add(int(sid))
    return out


def first_in_stage(conn: sqlite3.Connection, stage: str) -> Piece | None:
    """The oldest piece in this stage, or None (however many pieces there are)."""
    r = conn.execute(
        "SELECT * FROM studio_pieces WHERE stage = ? ORDER BY id LIMIT 1", (stage,)
    ).fetchone()
    return _row(r) if r else None


def recent_pieces(conn: sqlite3.Connection, limit: int) -> list[Piece]:
    """The newest pieces that got as far as being written (they define what to vary)."""
    rows = conn.execute(
        "SELECT * FROM studio_pieces WHERE angle != '' AND stage != ? ORDER BY id DESC LIMIT ?",
        (STAGE_DISCARDED, int(limit)),
    ).fetchall()
    return [_row(r) for r in rows]


def started_within(conn: sqlite3.Connection, days: float) -> list[Piece]:
    """The pieces started in the last `days` days that were not discarded, finished or not
    (waiting at the research checkpoint, stopped, failed), newest first: the topics a new
    piece is told not to repeat (`topics.avoid_days`). A piece still being made holds its
    topic as much as one in the queue does. Nothing for `days` of 0 or less."""
    if days <= 0:
        return []
    since = datetime.fromisoformat(_now()) - timedelta(days=days)
    rows = conn.execute(
        "SELECT * FROM studio_pieces WHERE created_at >= ? AND stage != ? ORDER BY id DESC",
        (since.isoformat(timespec="seconds"), STAGE_DISCARDED),
    ).fetchall()
    return [_row(r) for r in rows]


def pieces_since(conn: sqlite3.Connection, since_iso: str, origin: str | None = None) -> int:
    sql = "SELECT COUNT(*) FROM studio_pieces WHERE created_at >= ?"
    args: list[Any] = [since_iso]
    if origin:
        sql += " AND origin = ?"
        args.append(origin)
    return int(conn.execute(sql, args).fetchone()[0])


def last_voice(conn: sqlite3.Connection, exclude: int | None = None) -> str:
    """The voice of the newest piece that has one and was not discarded (`exclude`: the
    piece asking), '' when there is none: the voice a new piece is not given."""
    r = conn.execute(
        "SELECT voice FROM studio_pieces WHERE voice != '' AND stage != ? AND id != ?"
        " ORDER BY id DESC LIMIT 1",
        (STAGE_DISCARDED, -1 if exclude is None else int(exclude)),
    ).fetchone()
    return str(r[0]) if r else ""


def last_started(conn: sqlite3.Connection) -> str | None:
    r = conn.execute("SELECT MAX(created_at) FROM studio_pieces").fetchone()
    return r[0] if r and r[0] else None


def used_cluster_ids(conn: sqlite3.Connection) -> set[int]:
    rows = conn.execute(
        "SELECT cluster_id FROM studio_pieces WHERE cluster_id IS NOT NULL "
        "UNION SELECT cluster_id FROM studio_topics WHERE cluster_id IS NOT NULL"
    ).fetchall()
    return {int(r[0]) for r in rows}


def remembered_stories(conn: sqlite3.Connection) -> list[tuple[int, str]]:
    """Every (cluster id, story item) pair the studio's rows hold where the item is known:
    what runner.follow_merges needs to find a story that linking moved to another cluster."""
    rows = conn.execute(
        "SELECT cluster_id, story_item FROM studio_pieces "
        "WHERE cluster_id IS NOT NULL AND story_item != '' "
        "UNION SELECT cluster_id, story_item FROM studio_topics "
        "WHERE cluster_id IS NOT NULL AND story_item != '' ORDER BY 1, 2"
    ).fetchall()
    return [(int(r[0]), str(r[1])) for r in rows]


def repoint_story(conn: sqlite3.Connection, old: int, new: int) -> int:
    """Point every piece and queued topic on story `old` at `new`, the cluster story linking
    folded it into. Returns the rows changed."""
    n = 0
    for table in ("studio_pieces", "studio_topics"):
        cur = conn.execute(
            f"UPDATE {table} SET cluster_id = ? WHERE cluster_id = ?",  # noqa: S608
            (new, old),
        )
        n += cur.rowcount
    conn.commit()
    return n


# --- runs -----------------------------------------------------------------------------


def start_run(conn: sqlite3.Connection, piece_id: int, stage: str) -> int:
    cur = conn.execute(
        "INSERT INTO studio_runs (piece_id, stage, started_at) VALUES (?, ?, ?)",
        (piece_id, stage, _now()),
    )
    conn.commit()
    return int(cur.lastrowid)


def finish_run(
    conn: sqlite3.Connection,
    run_id: int,
    *,
    outcome: str,
    detail: str = "",
    turns: int = 0,
    cost_usd: float = 0.0,
    duration_ms: int = 0,
) -> None:
    conn.execute(
        """UPDATE studio_runs SET finished_at = ?, outcome = ?, detail = ?, turns = ?,
               cost_usd = ?, duration_ms = ? WHERE id = ?""",
        (_now(), outcome, detail[:2000], int(turns), float(cost_usd), int(duration_ms), run_id),
    )
    conn.commit()


@dataclass
class RunRow:
    id: int
    piece_id: int
    stage: str
    started_at: str
    finished_at: str | None
    outcome: str
    detail: str
    turns: int
    cost_usd: float
    duration_ms: int


def list_runs(conn: sqlite3.Connection, piece_id: int) -> list[RunRow]:
    rows = conn.execute(
        "SELECT * FROM studio_runs WHERE piece_id = ? ORDER BY id", (piece_id,)
    ).fetchall()
    return [
        RunRow(
            id=int(r["id"]),
            piece_id=int(r["piece_id"]),
            stage=r["stage"],
            started_at=r["started_at"],
            finished_at=r["finished_at"],
            outcome=r["outcome"],
            detail=r["detail"],
            turns=int(r["turns"]),
            cost_usd=float(r["cost_usd"]),
            duration_ms=int(r["duration_ms"]),
        )
        for r in rows
    ]


def close_open_runs(conn: sqlite3.Connection, piece_id: int, detail: str) -> None:
    conn.execute(
        "UPDATE studio_runs SET finished_at = ?, outcome = 'interrupted', detail = ? "
        "WHERE piece_id = ? AND finished_at IS NULL",
        (_now(), detail, piece_id),
    )
    conn.commit()


# --- queued topics ----------------------------------------------------------------------


@dataclass
class QueuedTopic:
    id: int
    created_at: str
    topic: str
    cluster_id: int | None
    angle: str
    checkpoint: bool
    piece_id: int | None
    story_item: str = ""  # one item of the story at cluster_id ('' when none is known)


def _topic_row(r: sqlite3.Row) -> QueuedTopic:
    return QueuedTopic(
        id=int(r["id"]),
        created_at=r["created_at"],
        topic=r["topic"],
        cluster_id=r["cluster_id"],
        angle=r["angle"],
        checkpoint=bool(r["checkpoint"]),
        piece_id=r["piece_id"],
        story_item=r["story_item"] or "",
    )


def queue_topic(
    conn: sqlite3.Connection,
    *,
    topic: str = "",
    cluster_id: int | None = None,
    angle: str = "",
    checkpoint: bool = True,
    story_item: str = "",
) -> int:
    if not topic.strip() and cluster_id is None:
        raise ValueError("a queued topic needs words or a story")
    cur = conn.execute(
        "INSERT INTO studio_topics (created_at, topic, cluster_id, story_item, angle, checkpoint) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (_now(), topic.strip(), cluster_id, story_item, angle.strip(), int(checkpoint)),
    )
    conn.commit()
    return int(cur.lastrowid)


def next_queued_topic(conn: sqlite3.Connection) -> QueuedTopic | None:
    r = conn.execute(
        "SELECT * FROM studio_topics WHERE piece_id IS NULL ORDER BY id LIMIT 1"
    ).fetchone()
    return _topic_row(r) if r is not None else None


def queued_topics(conn: sqlite3.Connection) -> list[QueuedTopic]:
    rows = conn.execute("SELECT * FROM studio_topics WHERE piece_id IS NULL ORDER BY id").fetchall()
    return [_topic_row(r) for r in rows]


def claim_topic(conn: sqlite3.Connection, topic_id: int, piece_id: int) -> None:
    """The queued topic is now this piece's; so is the radar topic or the catalyst it was
    queued from."""
    conn.execute("UPDATE studio_topics SET piece_id = ? WHERE id = ?", (piece_id, topic_id))
    conn.execute(
        "UPDATE studio_radar_topics SET piece_id = ?, status = ? WHERE topic_id = ?",
        (piece_id, RADAR_USED, topic_id),
    )
    conn.execute(
        "UPDATE studio_catalysts SET piece_id = ? WHERE topic_id = ?", (piece_id, topic_id)
    )
    conn.commit()


def drop_topic(conn: sqlite3.Connection, topic_id: int) -> None:
    """Drop a queued topic that has not started; a radar topic or a catalyst it was queued
    from goes back on the radar."""
    cur = conn.execute("DELETE FROM studio_topics WHERE id = ? AND piece_id IS NULL", (topic_id,))
    if cur.rowcount:
        conn.execute(
            "UPDATE studio_radar_topics SET status = ?, topic_id = NULL WHERE topic_id = ?",
            (RADAR_NEW, topic_id),
        )
        conn.execute("UPDATE studio_catalysts SET topic_id = NULL WHERE topic_id = ?", (topic_id,))
    conn.commit()


# --- playbook versions -------------------------------------------------------------------


@dataclass
class PlaybookVersion:
    id: int
    created_at: str
    source: str
    text: str
    changelog: list[str]
    evidence: str
    applied: bool
    based_on: int | None
    pieces: list[int] = field(default_factory=list)


def _json_list(raw: str | None) -> list[Any]:
    try:
        data = json.loads(raw or "[]")
    except ValueError:
        return []
    return data if isinstance(data, list) else []


def _version(r: sqlite3.Row) -> PlaybookVersion:
    changelog = _json_list(r["changelog"])
    return PlaybookVersion(
        id=int(r["id"]),
        created_at=r["created_at"],
        source=r["source"],
        text=r["text"],
        changelog=[str(c) for c in changelog],
        evidence=r["evidence"] or "",
        applied=bool(r["applied"]),
        based_on=r["based_on"],
        pieces=[int(x) for x in _json_list(r["pieces"]) if isinstance(x, int)],
    )


def add_playbook_version(
    conn: sqlite3.Connection,
    *,
    text: str,
    source: str,
    changelog: list[str] | tuple[str, ...] = (),
    evidence: str = "",
    based_on: int | None = None,
    pieces: list[int] | tuple[int, ...] = (),
) -> int:
    """Record a playbook. Every source but `proposal` is applied as it is written (the
    caller writes the file the sessions read: studio/playbook.py). `pieces`: the scored
    pieces a learned version or proposal was given, so the next rewrite waits for new ones."""
    if source not in PLAYBOOK_SOURCES:
        raise ValueError(f"unknown playbook source {source!r}")
    cur = conn.execute(
        "INSERT INTO studio_playbook_versions (created_at, source, text, changelog, evidence,"
        " applied, based_on, pieces) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            _now(),
            source,
            text,
            json.dumps(list(changelog)),
            evidence,
            int(source != PLAYBOOK_PROPOSAL),
            based_on,
            json.dumps([int(p) for p in pieces]),
        ),
    )
    conn.commit()
    return int(cur.lastrowid)


def playbook_versions(conn: sqlite3.Connection, limit: int = 50) -> list[PlaybookVersion]:
    """Newest first."""
    rows = conn.execute(
        "SELECT * FROM studio_playbook_versions ORDER BY id DESC LIMIT ?", (int(limit),)
    ).fetchall()
    return [_version(r) for r in rows]


def get_playbook_version(conn: sqlite3.Connection, version_id: int) -> PlaybookVersion | None:
    r = conn.execute(
        "SELECT * FROM studio_playbook_versions WHERE id = ?", (int(version_id),)
    ).fetchone()
    return _version(r) if r else None


def current_playbook_version(conn: sqlite3.Connection) -> PlaybookVersion | None:
    """The newest applied version: the one the sessions read."""
    r = conn.execute(
        "SELECT * FROM studio_playbook_versions WHERE applied = 1 ORDER BY id DESC LIMIT 1"
    ).fetchone()
    return _version(r) if r else None


def last_learned(conn: sqlite3.Connection) -> PlaybookVersion | None:
    """The learning loop's last rewrite, applied or proposed."""
    r = conn.execute(
        "SELECT * FROM studio_playbook_versions WHERE source IN (?, ?) ORDER BY id DESC LIMIT 1",
        (PLAYBOOK_LEARNED, PLAYBOOK_PROPOSAL),
    ).fetchone()
    return _version(r) if r else None


def open_proposal(conn: sqlite3.Connection) -> PlaybookVersion | None:
    """The newest learned proposal that is still waiting: not applied, and nothing applied
    after it (an editor's save or a revert since makes it stale)."""
    r = conn.execute(
        "SELECT * FROM studio_playbook_versions WHERE source = ? AND applied = 0"
        " AND id > COALESCE((SELECT MAX(id) FROM studio_playbook_versions WHERE applied = 1), 0)"
        " ORDER BY id DESC LIMIT 1",
        (PLAYBOOK_PROPOSAL,),
    ).fetchone()
    return _version(r) if r else None


def mark_playbook_applied(conn: sqlite3.Connection, version_id: int) -> None:
    conn.execute("UPDATE studio_playbook_versions SET applied = 1 WHERE id = ?", (version_id,))
    conn.commit()


# --- a post's numbers typed in by the editor --------------------------------------------------


def add_manual_metrics(
    conn: sqlite3.Connection,
    piece_id: int,
    counts: dict[str, int],
    captured_at: str | None = None,
) -> int:
    """The numbers X shows for a piece's first post, typed in by the editor (for an account
    without the X API read tier, or a post confirmed without its link)."""
    values = []
    for name in MANUAL_METRICS:
        n = int(counts.get(name) or 0)
        if n < 0:
            raise ValueError(f"{name} cannot be negative")
        values.append(n)
    cur = conn.execute(
        f"INSERT INTO studio_manual_metrics (piece_id, captured_at, {', '.join(MANUAL_METRICS)})"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (int(piece_id), captured_at or _now(), *values),
    )
    conn.commit()
    return int(cur.lastrowid)


def manual_metrics(conn: sqlite3.Connection) -> dict[int, list[dict[str, Any]]]:
    """Every typed-in snapshot by piece, oldest first: {piece_id: [{captured_at, counts}]}."""
    out: dict[int, list[dict[str, Any]]] = {}
    for r in conn.execute("SELECT * FROM studio_manual_metrics ORDER BY captured_at, id"):
        out.setdefault(int(r["piece_id"]), []).append(
            {"captured_at": r["captured_at"], "counts": {m: int(r[m]) for m in MANUAL_METRICS}}
        )
    return out


# --- read-only adapters onto other steps' tables ---------------------------------------------


@dataclass
class PostedHead:
    """The first post of a posted draft (any draft: studio or drafter) and every metrics
    snapshot step 4 took of it, oldest first."""

    draft_id: int
    item_id: str
    tweet_id: str
    posted_at: str
    text: str = ""  # what went out as the first post
    snapshots: list[dict[str, Any]] = field(default_factory=list)

    @property
    def measurable(self) -> bool:
        """A real X id: a post confirmed by hand without its link carries a marker instead,
        and step 4 can never fetch numbers for it."""
        return self.tweet_id.isdigit()


def _tables(conn: sqlite3.Connection) -> set[str]:
    return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}


def fetch_posted_heads(conn: sqlite3.Connection) -> list[PostedHead]:
    """Every posted head (step 3's `posts`, position 1, status 'posted', with a tweet id)
    with its draft's item_id (step 2's `drafts`) and its snapshots (step 4's
    `tweet_metrics`, deleted ones left out). Read-only; [] when `posts` is missing."""
    have = _tables(conn)
    if "posts" not in have:
        return []
    item = "d.item_id" if "drafts" in have else "''"
    join = "LEFT JOIN drafts d ON d.id = p.draft_id" if "drafts" in have else ""
    rows = conn.execute(
        f"""SELECT p.draft_id, {item} AS item_id, p.tweet_id, p.posted_at, p.text
            FROM posts p {join}
            WHERE p.position = 1 AND p.status = 'posted' AND p.tweet_id IS NOT NULL
              AND p.posted_at IS NOT NULL
            ORDER BY p.posted_at, p.id"""
    ).fetchall()
    heads = [
        PostedHead(int(r[0]), str(r[1] or ""), str(r[2]), str(r[3]), str(r[4] or "")) for r in rows
    ]
    if "tweet_metrics" in have and heads:
        by_tweet = {h.tweet_id: h for h in heads}
        for m in conn.execute(
            """SELECT tweet_id, captured_at, impressions, likes, reposts, replies, quotes,
                      bookmarks
               FROM tweet_metrics WHERE deleted = 0 ORDER BY tweet_id, captured_on, id"""
        ):
            head = by_tweet.get(str(m[0]))
            if head is not None:
                head.snapshots.append(
                    {
                        "captured_at": str(m[1]),
                        "counts": dict(
                            zip(MANUAL_METRICS, (int(v or 0) for v in m[2:8]), strict=True)
                        ),
                    }
                )
    return heads


def _decision_text(raw: str | None) -> str:
    """A decision's text as the queue logs it (JSON {"thread": [...]}, or older plain
    text), as one string."""
    if not raw:
        return ""
    try:
        data = json.loads(raw)
    except ValueError:
        return raw
    if isinstance(data, dict) and isinstance(data.get("thread"), list):
        return "\n\n".join(str(p) for p in data["thread"])
    return raw if not isinstance(data, str) else data


def fetch_studio_edits(conn: sqlite3.Connection, limit: int = 12) -> list[tuple[int, str, str]]:
    """The editor's hand edits of studio drafts, newest first: (piece id, before, after).
    Read-only on step 2's `drafts` and `decisions`; [] when either is missing."""
    if not {"drafts", "decisions"} <= _tables(conn):
        return []
    rows = conn.execute(
        """SELECT dr.item_id, de.original_text, de.edited_text
           FROM decisions de JOIN drafts dr ON dr.id = de.draft_id
           WHERE dr.item_id LIKE 'studio:%' AND de.action = 'edit'
             AND de.edited_text IS NOT NULL AND de.edited_text != de.original_text
           ORDER BY de.id DESC LIMIT ?""",
        (int(limit),),
    ).fetchall()
    out = []
    for item_id, before, after in rows:
        tail = str(item_id).split(":", 1)[1]
        if tail.isdigit():
            out.append((int(tail), _decision_text(before), _decision_text(after)))
    return out


@dataclass
class StudioReview:
    """What the editor did with one studio draft in the queue: where it stands, every hand
    edit that changed its text (before, after) and how many times the studio replaced its
    text on a request (`revise` decisions)."""

    piece_id: int
    draft_id: int
    status: str  # the draft's status: pending, approved, rejected
    posted: bool  # its first post is on X
    edits: list[tuple[str, str]] = field(default_factory=list)
    revisions: int = 0


def fetch_studio_reviews(conn: sqlite3.Connection) -> list[StudioReview]:
    """Every studio draft (item_id studio:<piece id>) with its decisions, oldest first.
    Read-only on step 2's `drafts` and `decisions` and step 3's `posts`; [] when the queue's
    tables are missing."""
    have = _tables(conn)
    if not {"drafts", "decisions"} <= have:
        return []
    posted: set[int] = set()
    if "posts" in have:
        posted = {
            int(r[0])
            for r in conn.execute(
                "SELECT DISTINCT draft_id FROM posts WHERE position = 1 AND status = 'posted'"
                " AND tweet_id IS NOT NULL"
            )
        }
    out: dict[int, StudioReview] = {}
    for draft_id, item_id, status in conn.execute(
        "SELECT id, item_id, status FROM drafts WHERE item_id LIKE 'studio:%' ORDER BY id"
    ):
        tail = str(item_id).split(":", 1)[1]
        if tail.isdigit():
            out[int(draft_id)] = StudioReview(
                piece_id=int(tail),
                draft_id=int(draft_id),
                status=str(status),
                posted=int(draft_id) in posted,
            )
    if not out:
        return []
    for draft_id, action, before, after in conn.execute(
        "SELECT draft_id, action, original_text, edited_text FROM decisions WHERE draft_id IN"
        " (SELECT id FROM drafts WHERE item_id LIKE 'studio:%') ORDER BY id"
    ):
        review = out.get(int(draft_id))
        if review is None:
            continue
        if action == "revise":
            review.revisions += 1
        elif action == "edit" and after is not None and after != before:
            old, new = _decision_text(before), _decision_text(after)
            if old != new:  # a dropped picture logs the same text twice
                review.edits.append((old, new))
    return list(out.values())


# --- the radar: scans, their topics, the catalyst calendar (studio/radar.py) ---------------

SCAN_RUNNING = "running"
SCAN_DONE = "done"
SCAN_FAILED = "failed"
RADAR_NEW = "new"
RADAR_QUEUED = "queued"  # the editor queued it as a topic; no piece yet
RADAR_USED = "used"  # a piece was written on it
RADAR_DISMISSED = "dismissed"
CATALYST_OPEN = "open"
CATALYST_DISMISSED = "dismissed"


@dataclass
class ScanRow:
    id: int
    started_at: str
    finished_at: str | None
    status: str
    model: str
    effort: str
    summary: str
    topics: int
    catalysts: int
    detail: str


def start_scan(conn: sqlite3.Connection, *, model: str, effort: str) -> int:
    cur = conn.execute(
        "INSERT INTO studio_scans (started_at, status, model, effort) VALUES (?, ?, ?, ?)",
        (_now(), SCAN_RUNNING, model, effort),
    )
    conn.commit()
    return int(cur.lastrowid)


def finish_scan(
    conn: sqlite3.Connection,
    scan_id: int,
    *,
    status: str,
    summary: str = "",
    topics: int = 0,
    catalysts: int = 0,
    detail: str = "",
) -> None:
    conn.execute(
        "UPDATE studio_scans SET finished_at = ?, status = ?, summary = ?, topics = ?,"
        " catalysts = ?, detail = ? WHERE id = ?",
        (_now(), status, summary, int(topics), int(catalysts), detail[:2000], scan_id),
    )
    conn.commit()


def _scan(r: sqlite3.Row) -> ScanRow:
    return ScanRow(
        id=int(r["id"]),
        started_at=r["started_at"],
        finished_at=r["finished_at"],
        status=r["status"],
        model=r["model"],
        effort=r["effort"],
        summary=r["summary"],
        topics=int(r["topics"]),
        catalysts=int(r["catalysts"]),
        detail=r["detail"],
    )


def last_scan(conn: sqlite3.Connection, status: str | None = None) -> ScanRow | None:
    """The newest scan (of that status)."""
    sql, args = "SELECT * FROM studio_scans", ()
    if status:
        sql, args = sql + " WHERE status = ?", (status,)
    r = conn.execute(sql + " ORDER BY id DESC LIMIT 1", args).fetchone()
    return _scan(r) if r else None


def close_running_scans(conn: sqlite3.Connection, detail: str) -> int:
    """Scans a dead run left `running` (this process holds the scan lock)."""
    cur = conn.execute(
        "UPDATE studio_scans SET status = ?, finished_at = ?, detail = ? WHERE status = ?",
        (SCAN_FAILED, _now(), detail, SCAN_RUNNING),
    )
    conn.commit()
    return cur.rowcount


@dataclass
class RadarTopic:
    id: int
    scan_id: int
    created_at: str
    rank: int
    title: str
    why_now: str
    angle: str
    companies: list[dict[str, str]]
    sources: list[str]
    status: str
    topic_id: int | None
    piece_id: int | None

    def to_topic(self) -> Any:
        from studio import radar

        return radar.Topic(
            title=self.title,
            why_now=self.why_now,
            angle=self.angle,
            companies=tuple(
                radar.Company(str(c.get("name") or ""), str(c.get("ticker") or ""))
                for c in self.companies
                if c.get("name")
            ),
            sources=tuple(self.sources),
        )


def _radar_topic(r: sqlite3.Row) -> RadarTopic:
    companies = [c for c in _json_list(r["companies"]) if isinstance(c, dict)]
    return RadarTopic(
        id=int(r["id"]),
        scan_id=int(r["scan_id"]),
        created_at=r["created_at"],
        rank=int(r["rank"]),
        title=r["title"],
        why_now=r["why_now"],
        angle=r["angle"],
        companies=[
            {"name": str(c.get("name") or ""), "ticker": str(c.get("ticker") or "")}
            for c in companies
        ],
        sources=[str(u) for u in _json_list(r["sources"]) if isinstance(u, str)],
        status=r["status"],
        topic_id=r["topic_id"],
        piece_id=r["piece_id"],
    )


def add_radar_topics(conn: sqlite3.Connection, scan_id: int, topics: list[Any]) -> list[int]:
    """A scan's topics (studio/radar.py:Topic), best first."""
    now, ids = _now(), []
    for rank, t in enumerate(topics, 1):
        cur = conn.execute(
            "INSERT INTO studio_radar_topics (scan_id, created_at, rank, title, why_now, angle,"
            " companies, sources) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                scan_id,
                now,
                rank,
                t.title,
                t.why_now,
                t.angle,
                json.dumps([{"name": c.name, "ticker": c.ticker} for c in t.companies]),
                json.dumps(list(t.sources)),
            ),
        )
        ids.append(int(cur.lastrowid))
    conn.commit()
    return ids


def get_radar_topic(conn: sqlite3.Connection, radar_id: int) -> RadarTopic | None:
    r = conn.execute("SELECT * FROM studio_radar_topics WHERE id = ?", (int(radar_id),)).fetchone()
    return _radar_topic(r) if r else None


def radar_topics(
    conn: sqlite3.Connection, *, since_iso: str, statuses: tuple[str, ...] = (RADAR_NEW,)
) -> list[RadarTopic]:
    """Topics from scans (and market movers, scan_id 0) since `since_iso`, newest first,
    best first within a scan."""
    marks = ", ".join("?" for _ in statuses)
    rows = conn.execute(
        f"SELECT * FROM studio_radar_topics WHERE created_at >= ? AND status IN ({marks})"
        " ORDER BY created_at DESC, scan_id DESC, rank, id",
        (since_iso, *statuses),
    ).fetchall()
    return [_radar_topic(r) for r in rows]


def set_radar_topic(
    conn: sqlite3.Connection,
    radar_id: int,
    *,
    status: str,
    topic_id: int | None = None,
    piece_id: int | None = None,
) -> None:
    sets, args = ["status = ?"], [status]
    if topic_id is not None:
        sets.append("topic_id = ?")
        args.append(topic_id)
    if piece_id is not None:
        sets.append("piece_id = ?")
        args.append(piece_id)
    conn.execute(
        f"UPDATE studio_radar_topics SET {', '.join(sets)} WHERE id = ?", (*args, int(radar_id))
    )
    conn.commit()


@dataclass
class CatalystRow:
    id: int
    key: str
    date_start: str
    date_end: str
    when_text: str
    company: str
    ticker: str
    drug: str
    kind: str
    detail: str
    source: str
    origin: str
    first_seen: str
    last_seen: str
    status: str
    topic_id: int | None
    piece_id: int | None

    def to_catalyst(self) -> Any:
        from datetime import date

        from studio import radar

        return radar.Catalyst(
            when=radar.When(
                date.fromisoformat(self.date_start),
                date.fromisoformat(self.date_end),
                self.when_text,
            ),
            company=self.company,
            ticker=self.ticker,
            drug=self.drug,
            kind=self.kind,
            detail=self.detail,
            source=self.source,
        )


def _catalyst(r: sqlite3.Row) -> CatalystRow:
    return CatalystRow(**{k: r[k] for k in r.keys()})  # noqa: SIM118 (sqlite3.Row)


def upsert_catalysts(
    conn: sqlite3.Connection, catalysts: list[Any], *, origin: str
) -> tuple[int, int]:
    """Add each catalyst (studio/radar.py:Catalyst) to the calendar, or, when its key is
    already there, note that it was seen again and fill in a detail or a source it lacked.
    A dismissed one stays dismissed. Returns (added, seen again)."""
    now, added, again = _now(), 0, 0
    for c in catalysts:
        cur = conn.execute(
            "UPDATE studio_catalysts SET last_seen = ?,"
            " detail = CASE WHEN detail = '' THEN ? ELSE detail END,"
            " source = CASE WHEN source = '' THEN ? ELSE source END,"
            " ticker = CASE WHEN ticker = '' THEN ? ELSE ticker END WHERE key = ?",
            (now, c.detail, c.source, c.ticker, c.key),
        )
        if cur.rowcount:
            again += 1
            continue
        conn.execute(
            "INSERT INTO studio_catalysts (key, date_start, date_end, when_text, company, ticker,"
            " drug, kind, detail, source, origin, first_seen, last_seen)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                c.key,
                c.when.start.isoformat(),
                c.when.end.isoformat(),
                c.when.text,
                c.company,
                c.ticker,
                c.drug,
                c.kind,
                c.detail,
                c.source,
                origin,
                now,
                now,
            ),
        )
        added += 1
    conn.commit()
    return added, again


def catalysts_between(
    conn: sqlite3.Connection,
    first_day: str,
    last_day: str,
    *,
    statuses: tuple[str, ...] = (CATALYST_OPEN,),
) -> list[CatalystRow]:
    """Catalysts that may fall between two ISO dates (a quarter counts while any of it is
    in the span), soonest first."""
    marks = ", ".join("?" for _ in statuses)
    rows = conn.execute(
        f"SELECT * FROM studio_catalysts WHERE date_end >= ? AND date_start <= ?"
        f" AND status IN ({marks}) ORDER BY date_start, date_end, company, id",
        (first_day, last_day, *statuses),
    ).fetchall()
    return [_catalyst(r) for r in rows]


def get_catalyst(conn: sqlite3.Connection, catalyst_id: int) -> CatalystRow | None:
    r = conn.execute("SELECT * FROM studio_catalysts WHERE id = ?", (int(catalyst_id),)).fetchone()
    return _catalyst(r) if r else None


def set_catalyst(
    conn: sqlite3.Connection,
    catalyst_id: int,
    *,
    status: str | None = None,
    topic_id: int | None = None,
) -> None:
    sets, args = [], []
    if status is not None:
        sets.append("status = ?")
        args.append(status)
    if topic_id is not None:
        sets.append("topic_id = ?")
        args.append(topic_id)
    if sets:
        conn.execute(
            f"UPDATE studio_catalysts SET {', '.join(sets)} WHERE id = ?", (*args, int(catalyst_id))
        )
        conn.commit()


def covered_since(conn: sqlite3.Connection, since_iso: str) -> list[Any]:
    """What a new radar topic or catalyst may repeat (studio/repeats.py:Covered), newest
    first: every piece started since `since_iso` that was not discarded, and every topic
    still waiting in the queue. Each brings the companies, sources and drug of the radar
    topic or catalyst it was queued from, when it was."""
    from studio.repeats import Covered

    radar: dict[tuple[str, int], RadarTopic] = {}
    for r in conn.execute(
        "SELECT * FROM studio_radar_topics WHERE piece_id IS NOT NULL OR topic_id IS NOT NULL"
    ).fetchall():
        t = _radar_topic(r)
        if t.piece_id is not None:
            radar[("piece", t.piece_id)] = t
        if t.topic_id is not None:
            radar[("queued", t.topic_id)] = t
    cats: dict[tuple[str, int], CatalystRow] = {}
    for r in conn.execute(
        "SELECT * FROM studio_catalysts WHERE piece_id IS NOT NULL OR topic_id IS NOT NULL"
    ).fetchall():
        c = _catalyst(r)
        if c.piece_id is not None:
            cats[("piece", c.piece_id)] = c
        if c.topic_id is not None:
            cats[("queued", c.topic_id)] = c

    def covered(kind: str, id_: int, label: str, angle: str, status: str, text: str) -> Any:
        t, c = radar.get((kind, id_)), cats.get((kind, id_))
        companies = [(x["name"], x["ticker"]) for x in t.companies] if t else []
        sources = list(t.sources) if t else []
        if c is not None:
            companies.append((c.company, c.ticker))
            sources += [c.source] if c.source else []
        return Covered(
            kind=kind,
            id=id_,
            label=label,
            angle=angle,
            status=status,
            text=text,
            companies=tuple(companies),
            sources=tuple(sources),
            drugs=(c.drug,) if c is not None and c.drug else (),
        )

    out = []
    for t in reversed(queued_topics(conn)):
        first = t.topic.strip().splitlines()[0] if t.topic.strip() else f"topic {t.id}"
        out.append(covered("queued", t.id, first, t.angle, "queued", t.topic))
    rows = conn.execute(
        "SELECT * FROM studio_pieces WHERE created_at >= ? AND stage != ? ORDER BY id DESC",
        (since_iso, STAGE_DISCARDED),
    ).fetchall()
    for p in map(_row, rows):
        angle = p.angle or p.requested_angle
        out.append(covered("piece", p.id, p.label, angle, p.stage, f"{p.title}\n{p.topic}"))
    return out
