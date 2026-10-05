"""The studio's tables: studio_pieces (one per piece, with its stage and session),
studio_runs (one per CLI invocation) and studio_topics (topics a human queued).

The studio owns these three tables and nothing else. It reads step 1 only through
studio/topics.py and writes into the approval queue only through studio/ingest.py,
which uses approval_queue/store.py's own functions.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime
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
    angle      TEXT NOT NULL DEFAULT '',
    checkpoint INTEGER NOT NULL DEFAULT 1,
    piece_id   INTEGER
);
"""


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def ensure_tables(conn: sqlite3.Connection) -> None:
    conn.executescript(_SCHEMA)
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
    meta: dict[str, Any] = field(default_factory=dict)

    @property
    def label(self) -> str:
        return self.title or self.topic or f"piece {self.id}"


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
) -> int:
    now = _now()
    cur = conn.execute(
        """INSERT INTO studio_pieces (created_at, updated_at, origin, topic, cluster_id,
               requested_angle, stage, checkpoint, session_id, workspace, model, effort)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            now,
            now,
            origin,
            topic,
            cluster_id,
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
    "session_id",
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


def pieces_since(conn: sqlite3.Connection, since_iso: str, origin: str | None = None) -> int:
    sql = "SELECT COUNT(*) FROM studio_pieces WHERE created_at >= ?"
    args: list[Any] = [since_iso]
    if origin:
        sql += " AND origin = ?"
        args.append(origin)
    return int(conn.execute(sql, args).fetchone()[0])


def last_started(conn: sqlite3.Connection) -> str | None:
    r = conn.execute("SELECT MAX(created_at) FROM studio_pieces").fetchone()
    return r[0] if r and r[0] else None


def used_cluster_ids(conn: sqlite3.Connection) -> set[int]:
    rows = conn.execute(
        "SELECT cluster_id FROM studio_pieces WHERE cluster_id IS NOT NULL "
        "UNION SELECT cluster_id FROM studio_topics WHERE cluster_id IS NOT NULL"
    ).fetchall()
    return {int(r[0]) for r in rows}


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


def queue_topic(
    conn: sqlite3.Connection,
    *,
    topic: str = "",
    cluster_id: int | None = None,
    angle: str = "",
    checkpoint: bool = True,
) -> int:
    if not topic.strip() and cluster_id is None:
        raise ValueError("a queued topic needs words or a story")
    cur = conn.execute(
        "INSERT INTO studio_topics (created_at, topic, cluster_id, angle, checkpoint) "
        "VALUES (?, ?, ?, ?, ?)",
        (_now(), topic.strip(), cluster_id, angle.strip(), int(checkpoint)),
    )
    conn.commit()
    return int(cur.lastrowid)


def next_queued_topic(conn: sqlite3.Connection) -> QueuedTopic | None:
    r = conn.execute(
        "SELECT * FROM studio_topics WHERE piece_id IS NULL ORDER BY id LIMIT 1"
    ).fetchone()
    if r is None:
        return None
    return QueuedTopic(
        id=int(r["id"]),
        created_at=r["created_at"],
        topic=r["topic"],
        cluster_id=r["cluster_id"],
        angle=r["angle"],
        checkpoint=bool(r["checkpoint"]),
        piece_id=r["piece_id"],
    )


def queued_topics(conn: sqlite3.Connection) -> list[QueuedTopic]:
    rows = conn.execute("SELECT * FROM studio_topics WHERE piece_id IS NULL ORDER BY id").fetchall()
    return [
        QueuedTopic(
            id=int(r["id"]),
            created_at=r["created_at"],
            topic=r["topic"],
            cluster_id=r["cluster_id"],
            angle=r["angle"],
            checkpoint=bool(r["checkpoint"]),
            piece_id=r["piece_id"],
        )
        for r in rows
    ]


def claim_topic(conn: sqlite3.Connection, topic_id: int, piece_id: int) -> None:
    conn.execute("UPDATE studio_topics SET piece_id = ? WHERE id = ?", (piece_id, topic_id))
    conn.commit()


def drop_topic(conn: sqlite3.Connection, topic_id: int) -> None:
    conn.execute("DELETE FROM studio_topics WHERE id = ? AND piece_id IS NULL", (topic_id,))
    conn.commit()
