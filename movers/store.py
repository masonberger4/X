"""Market movers' own tables: `movers_runs` (one row per screen) and `movers_moves` (one
row per ticker, session and day it moved, so a move is checked once however often the
screen runs). What goes to the studio is written through studio/store.py's own functions
(a radar topic, a queued topic); this module never writes a studio table itself."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime

SCHEMA = """
CREATE TABLE IF NOT EXISTS movers_runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    status      TEXT NOT NULL DEFAULT 'running',
    screened    INTEGER NOT NULL DEFAULT 0,
    failed      INTEGER NOT NULL DEFAULT 0,
    flagged     INTEGER NOT NULL DEFAULT 0,
    checked     INTEGER NOT NULL DEFAULT 0,
    topics      INTEGER NOT NULL DEFAULT 0,
    queued      INTEGER NOT NULL DEFAULT 0,
    summary     TEXT NOT NULL DEFAULT '',
    detail      TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS movers_moves (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id        INTEGER NOT NULL,
    created_at    TEXT NOT NULL,
    ticker        TEXT NOT NULL,
    company       TEXT NOT NULL DEFAULT '',
    session       TEXT NOT NULL,
    day           TEXT NOT NULL,
    pct           REAL NOT NULL,
    price_from    REAL NOT NULL,
    price_to      REAL NOT NULL,
    volume        REAL NOT NULL DEFAULT 0,
    status        TEXT NOT NULL,
    cause         TEXT NOT NULL DEFAULT '',
    what_happened TEXT NOT NULL DEFAULT '',
    title         TEXT NOT NULL DEFAULT '',
    radar_id      INTEGER,
    UNIQUE (ticker, session, day)
);
CREATE INDEX IF NOT EXISTS idx_movers_moves_ticker ON movers_moves(ticker, created_at);
"""

RUN_RUNNING = "running"
RUN_DONE = "done"
RUN_FAILED = "failed"

STORY = "story"  # a cause was found, worth a piece (on the radar)
NOTED = "noted"  # a cause was found, not worth a piece
NO_STORY = "no_story"  # no cause found
UNCHECKED = "unchecked"  # over the per-call cap, or the check gave no answer for it


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def ensure_tables(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.commit()


@dataclass
class RunRow:
    id: int
    started_at: str
    finished_at: str | None
    status: str
    summary: str
    detail: str


def start_run(conn: sqlite3.Connection) -> int:
    cur = conn.execute("INSERT INTO movers_runs (started_at) VALUES (?)", (_now(),))
    conn.commit()
    return int(cur.lastrowid)


def finish_run(conn: sqlite3.Connection, run_id: int, *, status: str, **counts: object) -> None:
    allowed = {"screened", "failed", "flagged", "checked", "topics", "queued", "summary", "detail"}
    sets, args = ["finished_at = ?", "status = ?"], [_now(), status]
    for key, value in counts.items():
        if key not in allowed:
            raise KeyError(key)
        sets.append(f"{key} = ?")
        args.append(value)
    conn.execute(f"UPDATE movers_runs SET {', '.join(sets)} WHERE id = ?", (*args, run_id))
    conn.commit()


def last_run(conn: sqlite3.Connection, status: str | None = None) -> RunRow | None:
    sql, args = "SELECT * FROM movers_runs", ()
    if status:
        sql, args = sql + " WHERE status = ?", (status,)
    r = conn.execute(sql + " ORDER BY id DESC LIMIT 1", args).fetchone()
    if r is None:
        return None
    return RunRow(
        int(r["id"]), r["started_at"], r["finished_at"], r["status"], r["summary"], r["detail"]
    )


def close_running(conn: sqlite3.Connection, detail: str) -> int:
    cur = conn.execute(
        "UPDATE movers_runs SET status = ?, finished_at = ?, detail = ? WHERE status = ?",
        (RUN_FAILED, _now(), detail, RUN_RUNNING),
    )
    conn.commit()
    return cur.rowcount


def seen(conn: sqlite3.Connection, ticker: str, session: str, day: str) -> bool:
    r = conn.execute(
        "SELECT 1 FROM movers_moves WHERE ticker = ? AND session = ? AND day = ?",
        (ticker, session, day),
    ).fetchone()
    return r is not None


def add_move(
    conn: sqlite3.Connection,
    run_id: int,
    *,
    ticker: str,
    company: str,
    session: str,
    day: str,
    pct: float,
    price_from: float,
    price_to: float,
    volume: float,
    status: str,
    cause: str = "",
    what_happened: str = "",
    title: str = "",
    radar_id: int | None = None,
) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO movers_moves (run_id, created_at, ticker, company, session, day,"
        " pct, price_from, price_to, volume, status, cause, what_happened, title, radar_id)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            run_id,
            _now(),
            ticker,
            company,
            session,
            day,
            pct,
            price_from,
            price_to,
            volume,
            status,
            cause,
            what_happened,
            title,
            radar_id,
        ),
    )
    conn.commit()


def earlier_stories(conn: sqlite3.Connection, since_iso: str) -> dict[str, list[str]]:
    """ticker -> the causes found for its moves since `since_iso`, newest first, once each."""
    rows = conn.execute(
        "SELECT ticker, day, title, what_happened FROM movers_moves WHERE created_at >= ?"
        " AND status IN (?, ?) ORDER BY created_at DESC, id DESC",
        (since_iso, STORY, NOTED),
    ).fetchall()
    out: dict[str, list[str]] = {}
    for r in rows:
        line = f"{r['day']}: {r['title'] or r['what_happened']}"
        if line not in out.setdefault(r["ticker"], []):
            out[r["ticker"]].append(line)
    return out
