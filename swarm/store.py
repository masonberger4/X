"""Step 9's own tables on the pipeline database. It reads no other step's table.

swarm_genomes   the heritable part (swarm/genome.py), one row per genome; phase three adds
                children with parent_id, phase two retires losers (retired_at)
swarm_runs      one row per story the swarm was tried on: which genome, which variant won,
                how many model calls, and the full cell/tournament log
swarm_variants  the swarm and control drafts of a run as JSON, whether each passed the hard
                rules, so phase two can score the jury against X
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime

from draft.schema import Draft
from swarm.genome import DEFAULT_GENOME, Genome

ROLES = ("swarm", "control")


def _now() -> str:
    return datetime.now(UTC).isoformat()


def ensure_tables(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS swarm_genomes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            genome_json TEXT NOT NULL,
            parent_id INTEGER,
            created_at TEXT NOT NULL,
            retired_at TEXT
        );
        CREATE TABLE IF NOT EXISTS swarm_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            draft_id INTEGER,
            item_id TEXT NOT NULL,
            cluster_id INTEGER,
            genome_id INTEGER,
            winner TEXT,
            calls INTEGER NOT NULL DEFAULT 0,
            log_json TEXT,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS swarm_variants (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id INTEGER NOT NULL REFERENCES swarm_runs(id),
            role TEXT NOT NULL,
            model TEXT,
            draft_json TEXT,
            ok INTEGER NOT NULL DEFAULT 0,
            problems TEXT,
            created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_swarm_runs_draft ON swarm_runs(draft_id);
        """
    )
    conn.commit()


def seed_default(conn: sqlite3.Connection) -> int:
    """Insert DEFAULT_GENOME when the table is empty; return the active genome's id."""
    row = conn.execute("SELECT id FROM swarm_genomes WHERE retired_at IS NULL LIMIT 1").fetchone()
    if row:
        return int(row[0])
    cur = conn.execute(
        "INSERT INTO swarm_genomes (name, genome_json, parent_id, created_at) VALUES (?, ?, ?, ?)",
        (DEFAULT_GENOME.name, DEFAULT_GENOME.to_json(), None, _now()),
    )
    conn.commit()
    return int(cur.lastrowid)


def active_genome(conn: sqlite3.Connection) -> Genome:
    """The newest unretired genome (seeded if none)."""
    seed_default(conn)
    row = conn.execute(
        "SELECT id, genome_json FROM swarm_genomes WHERE retired_at IS NULL "
        "ORDER BY id DESC LIMIT 1"
    ).fetchone()
    return Genome.from_json(row[1], id=int(row[0]))


def list_genomes(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    conn.row_factory = sqlite3.Row
    return conn.execute("SELECT * FROM swarm_genomes ORDER BY id").fetchall()


def record_run(
    conn: sqlite3.Connection,
    *,
    item_id: str,
    cluster_id: int | None,
    genome_id: int | None,
    winner: str | None,
    calls: int,
    log: list[dict] | dict | None,
    draft_id: int | None = None,
) -> int:
    cur = conn.execute(
        """INSERT INTO swarm_runs (draft_id, item_id, cluster_id, genome_id, winner, calls,
                                   log_json, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            draft_id,
            item_id,
            cluster_id,
            genome_id,
            winner,
            calls,
            json.dumps(log, ensure_ascii=False) if log is not None else None,
            _now(),
        ),
    )
    conn.commit()
    return int(cur.lastrowid)


def set_run_draft(conn: sqlite3.Connection, run_id: int, draft_id: int) -> None:
    conn.execute("UPDATE swarm_runs SET draft_id = ? WHERE id = ?", (draft_id, run_id))
    conn.commit()


def record_variant(
    conn: sqlite3.Connection,
    run_id: int,
    *,
    role: str,
    model: str | None,
    draft: Draft | None,
    problems: list[str] | None = None,
) -> int:
    if role not in ROLES:
        raise ValueError(f"unknown role {role!r}")
    cur = conn.execute(
        """INSERT INTO swarm_variants (run_id, role, model, draft_json, ok, problems, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (
            run_id,
            role,
            model,
            json.dumps(draft.to_dict(), ensure_ascii=False) if draft is not None else None,
            0 if problems else 1,
            "; ".join(problems) if problems else None,
            _now(),
        ),
    )
    conn.commit()
    return int(cur.lastrowid)


@dataclass
class RunRow:
    id: int
    draft_id: int | None
    item_id: str
    genome_id: int | None
    winner: str | None
    calls: int


def list_runs(conn: sqlite3.Connection, limit: int = 50) -> list[RunRow]:
    rows = conn.execute(
        "SELECT id, draft_id, item_id, genome_id, winner, calls FROM swarm_runs "
        "ORDER BY id DESC LIMIT ?",
        (limit,),
    ).fetchall()
    return [RunRow(*r) for r in rows]


def list_variants(conn: sqlite3.Connection, run_id: int) -> list[sqlite3.Row]:
    conn.row_factory = sqlite3.Row
    return conn.execute(
        "SELECT * FROM swarm_variants WHERE run_id = ? ORDER BY id", (run_id,)
    ).fetchall()
