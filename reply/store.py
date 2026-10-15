"""Reply storage: one row per post the editor asked a reply for, and its screenshots.

Owns one table, created with CREATE TABLE IF NOT EXISTS in the shared pipeline DB:

  reply_requests(id PK, created_at, updated_at,
                 status 'writing'|'ready'|'skipped'|'blocked'|'failed'|'posted'|'discarded',
                 shots_json (file names under shot_dir()), post_url, own_url, note,
                 author, post_text (as the writer read them off the screenshots),
                 reply, why, problems_json, error, model, attempts, draft_id)

`own_url` is the link of the account's own post the screenshots answer (someone replied to
or quoted it). `own_post` finds that post in step 3's `posts` by its X id (read-only) and,
when its draft is a studio piece, the piece's folder in `studio_pieces`, so the writer can
reply from what the account said and from the piece's fact base.

The screenshots live in `<db folder>/reply_shots/` (`shot_dir()`), named
`req<id>_<k>.<ext>`. A reply becomes a draft in step 2's `drafts` only when the editor says
it is posted (`web.py`, through approval_queue/store.py): item_id `reply:req<id>`
(`approval_queue/store.py:reply_item_id`), shape single, the post it answers in
Draft.reply_to. Step 3's own module then logs it as posted, so step 4 tracks it.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

from approval_queue import store as queue_store

WRITING = "writing"
READY = "ready"
SKIPPED = "skipped"
BLOCKED = "blocked"  # the reply still failed the checks after its attempts
FAILED = "failed"  # the call itself failed (the CLI, a timeout, an unreadable answer)
POSTED = "posted"
DISCARDED = "discarded"
#: What a request may be written again from (a fresh attempt with the editor's note).
REWRITABLE = (READY, SKIPPED, BLOCKED, FAILED)

SHOTS_DIRNAME = "reply_shots"
#: Picture types a screenshot may be, by the extension it is stored under.
SHOT_TYPES = {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp", "image/gif": "gif"}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS reply_requests (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL,
    status        TEXT NOT NULL,
    shots_json    TEXT NOT NULL DEFAULT '[]',
    post_url      TEXT NOT NULL DEFAULT '',
    note          TEXT NOT NULL DEFAULT '',
    author        TEXT NOT NULL DEFAULT '',
    post_text     TEXT NOT NULL DEFAULT '',
    reply         TEXT NOT NULL DEFAULT '',
    why           TEXT NOT NULL DEFAULT '',
    problems_json TEXT NOT NULL DEFAULT '[]',
    error         TEXT NOT NULL DEFAULT '',
    model         TEXT NOT NULL DEFAULT '',
    attempts      INTEGER NOT NULL DEFAULT 0,
    draft_id      INTEGER
);
CREATE INDEX IF NOT EXISTS idx_reply_requests_created ON reply_requests(created_at);
"""

#: Columns added after the table first shipped (guarded migration in `connect`).
_ADDED = {"own_url": "TEXT NOT NULL DEFAULT ''"}


def connect(path: str | Path | None = None) -> sqlite3.Connection:
    conn = queue_store.connect(path)
    conn.executescript(_SCHEMA)
    have = {r[1] for r in conn.execute("PRAGMA table_info(reply_requests)").fetchall()}
    for column, decl in _ADDED.items():
        if column not in have:
            conn.execute(f"ALTER TABLE reply_requests ADD COLUMN {column} {decl}")
    conn.commit()
    return conn


def shot_dir() -> Path:
    """Where the screenshots live: a `reply_shots` folder next to the pipeline DB."""
    return queue_store.db_path().resolve().parent / SHOTS_DIRNAME


def _now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


@dataclass
class Request:
    id: int
    created_at: str
    updated_at: str
    status: str
    shots: list[str]
    post_url: str = ""
    own_url: str = ""
    note: str = ""
    author: str = ""
    post_text: str = ""
    reply: str = ""
    why: str = ""
    problems: list[str] = field(default_factory=list)
    error: str = ""
    model: str = ""
    attempts: int = 0
    draft_id: int | None = None

    @property
    def rewritable(self) -> bool:
        return self.status in REWRITABLE

    def shot_paths(self) -> list[Path]:
        return [shot_dir() / name for name in self.shots]


def _row(r: sqlite3.Row) -> Request:
    def listed(text: str) -> list[str]:
        try:
            data = json.loads(text or "[]")
        except ValueError:
            return []
        return [str(x) for x in data] if isinstance(data, list) else []

    return Request(
        id=int(r["id"]),
        created_at=r["created_at"],
        updated_at=r["updated_at"],
        status=r["status"],
        shots=listed(r["shots_json"]),
        post_url=r["post_url"],
        own_url=r["own_url"],
        note=r["note"],
        author=r["author"],
        post_text=r["post_text"],
        reply=r["reply"],
        why=r["why"],
        problems=listed(r["problems_json"]),
        error=r["error"],
        model=r["model"],
        attempts=int(r["attempts"]),
        draft_id=r["draft_id"],
    )


def create(
    conn: sqlite3.Connection,
    shots: list[tuple[str, bytes]],
    *,
    post_url: str = "",
    own_url: str = "",
    note: str = "",
) -> int:
    """A new request in `writing`, its screenshots saved (`shots`: [(extension, bytes)])."""
    now = _now()
    cur = conn.execute(
        "INSERT INTO reply_requests (created_at, updated_at, status, post_url, own_url, note) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (now, now, WRITING, post_url.strip(), own_url.strip(), note.strip()),
    )
    rid = int(cur.lastrowid)
    folder = shot_dir()
    folder.mkdir(parents=True, exist_ok=True)
    names = []
    for k, (ext, data) in enumerate(shots, 1):
        name = f"req{rid}_{k}.{ext}"
        (folder / name).write_bytes(data)
        names.append(name)
    conn.execute("UPDATE reply_requests SET shots_json = ? WHERE id = ?", (json.dumps(names), rid))
    conn.commit()
    return rid


def get(conn: sqlite3.Connection, rid: int) -> Request | None:
    r = conn.execute("SELECT * FROM reply_requests WHERE id = ?", (rid,)).fetchone()
    return _row(r) if r else None


def recent(conn: sqlite3.Connection, limit: int = 30) -> list[Request]:
    rows = conn.execute(
        "SELECT * FROM reply_requests ORDER BY id DESC LIMIT ?", (limit,)
    ).fetchall()
    return [_row(r) for r in rows]


def _update(conn: sqlite3.Connection, rid: int, **fields) -> None:
    fields["updated_at"] = _now()
    cols = ", ".join(f"{k} = ?" for k in fields)
    conn.execute(f"UPDATE reply_requests SET {cols} WHERE id = ?", (*fields.values(), rid))
    conn.commit()


def start_rewrite(conn: sqlite3.Connection, rid: int, note: str) -> bool:
    """Back to `writing` with the editor's new note; False unless it may be written again."""
    cur = conn.execute(
        f"UPDATE reply_requests SET status = ?, note = ?, error = '', updated_at = ? "
        f"WHERE id = ? AND status IN ({','.join('?' * len(REWRITABLE))})",
        (WRITING, note.strip(), _now(), rid, *REWRITABLE),
    )
    conn.commit()
    return cur.rowcount == 1


def finish(
    conn: sqlite3.Connection,
    rid: int,
    *,
    status: str,
    author: str = "",
    post_text: str = "",
    reply: str = "",
    why: str = "",
    problems: list[str] | None = None,
    error: str = "",
    model: str = "",
    attempts: int = 0,
) -> None:
    """Record what one writing run came back with. A run whose request was discarded or
    posted meanwhile changes nothing."""
    row = get(conn, rid)
    if row is None or row.status != WRITING:
        return
    _update(
        conn,
        rid,
        status=status,
        author=author or row.author,
        post_text=post_text or row.post_text,
        reply=reply,
        why=why,
        problems_json=json.dumps(problems or []),
        error=error,
        model=model or row.model,
        attempts=row.attempts + attempts,
    )


def set_reply_text(conn: sqlite3.Connection, rid: int, text: str) -> None:
    _update(conn, rid, reply=text)


def mark(conn: sqlite3.Connection, rid: int, status: str, draft_id: int | None = None) -> None:
    if draft_id is None:
        _update(conn, rid, status=status)
    else:
        _update(conn, rid, status=status, draft_id=draft_id)


def settle_dead(conn: sqlite3.Connection, older_than_minutes: float) -> int:
    """Requests left `writing` by an app that stopped mid-call: failed, so they can be
    written again. Only those untouched for longer than a call can take."""
    cutoff = (datetime.now(UTC) - timedelta(minutes=older_than_minutes)).replace(microsecond=0)
    cur = conn.execute(
        "UPDATE reply_requests SET status = ?, error = ?, updated_at = ? "
        "WHERE status = ? AND updated_at < ?",
        (
            FAILED,
            "the app stopped while this was being written",
            _now(),
            WRITING,
            cutoff.isoformat(),
        ),
    )
    conn.commit()
    return cur.rowcount


@dataclass(frozen=True)
class OwnPost:
    """The account's own post a reply is about: the draft it went out as, every post of it
    as it went out, and its studio piece's folder when a studio piece wrote it."""

    draft_id: int
    texts: list[str]
    piece_id: int | None = None
    workspace: str = ""


def _tables(conn: sqlite3.Connection) -> set[str]:
    return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}


def own_post(conn: sqlite3.Connection, tweet_id: str) -> OwnPost | None:
    """The account's post with this X id, as step 3 logged it (any post of a thread), or None
    when no posted row carries it. Read-only, through step 3's `posts`, step 2's `drafts`
    and the studio's `studio_pieces`; None when `posts` is missing."""
    have = _tables(conn)
    if not tweet_id or "posts" not in have:
        return None
    row = conn.execute(
        "SELECT draft_id FROM posts WHERE tweet_id = ? AND status = 'posted' LIMIT 1",
        (str(tweet_id),),
    ).fetchone()
    if row is None:
        return None
    draft_id = int(row[0])
    texts = [
        str(r[0] or "")
        for r in conn.execute(
            "SELECT text FROM posts WHERE draft_id = ? AND status = 'posted' ORDER BY position, id",
            (draft_id,),
        )
    ]
    piece_id, workspace = None, ""
    if "drafts" in have:
        item = conn.execute("SELECT item_id FROM drafts WHERE id = ?", (draft_id,)).fetchone()
        prefix = queue_store.STUDIO_ITEM_PREFIX
        tail = str(item[0] or "")[len(prefix) :] if item else ""
        if item and str(item[0] or "").startswith(prefix) and tail.isdigit():
            piece_id = int(tail)
            if "studio_pieces" in have:
                ws = conn.execute(
                    "SELECT workspace FROM studio_pieces WHERE id = ?", (piece_id,)
                ).fetchone()
                workspace = str(ws[0] or "") if ws else ""
    return OwnPost(draft_id, texts, piece_id, workspace)
