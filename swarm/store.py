"""Step 9's own tables on the pipeline database. It reads no other step's table.

swarm_genomes   the heritable part (swarm/genome.py), one row per genome; phase three adds
                children with parent_id, phase two retires losers (retired_at)
swarm_runs      one row per story the swarm was tried on: which genome, which variant won,
                how many model calls, and the full cell/tournament log
swarm_variants  the swarm and control drafts of a run as JSON, whether each passed the hard
                rules, so phase two can score the jury against X
swarm_fitness   phase two: one row per posted run, the head tweet's KPI, the trailing
                baseline and the relative score (run_evolve.py)

fetch_head_metrics is the ONE place step 9 reads another step's tables (step 3's posts
and step 4's tweet_metrics), read-only, empty when either table is missing.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime

from draft.schema import Draft
from swarm.genome import SEED_GENOMES, Genome

ROLES = ("swarm", "control")


def _tables(conn: sqlite3.Connection) -> set[str]:
    return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}


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
        CREATE TABLE IF NOT EXISTS swarm_fitness (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id INTEGER NOT NULL UNIQUE REFERENCES swarm_runs(id),
            draft_id INTEGER NOT NULL,
            genome_id INTEGER,
            winner TEXT,
            tweet_id TEXT NOT NULL,
            posted_at TEXT NOT NULL,
            kpi TEXT NOT NULL,
            value REAL NOT NULL,
            baseline REAL,
            relative REAL,
            computed_at TEXT NOT NULL
        );
        """
    )
    if "retired_reason" not in _columns(conn, "swarm_genomes"):  # guarded migration
        conn.execute("ALTER TABLE swarm_genomes ADD COLUMN retired_reason TEXT")
    conn.commit()


def seed_default(conn: sqlite3.Connection) -> int:
    """Insert every SEED_GENOME whose name is not yet in the table (a retired seed is not
    re-added). Returns the id of the first seed (the default genome)."""
    names = {r[0]: int(r[1]) for r in conn.execute("SELECT name, id FROM swarm_genomes")}
    for g in SEED_GENOMES:
        if g.name in names:
            continue
        cur = conn.execute(
            "INSERT INTO swarm_genomes (name, genome_json, parent_id, created_at) "
            "VALUES (?, ?, ?, ?)",
            (g.name, g.to_json(), None, _now()),
        )
        names[g.name] = int(cur.lastrowid)
    conn.commit()
    return names[SEED_GENOMES[0].name]


def _genome_row(row: sqlite3.Row | tuple) -> Genome:
    return Genome.from_json(row[1], id=int(row[0]))


def active_genome(conn: sqlite3.Connection) -> Genome:
    """The newest unretired genome (seeded if none)."""
    seed_default(conn)
    row = conn.execute(
        "SELECT id, genome_json FROM swarm_genomes WHERE retired_at IS NULL "
        "ORDER BY id DESC LIMIT 1"
    ).fetchone()
    return _genome_row(row)


def next_genome(conn: sqlite3.Connection) -> Genome:
    """Round-robin: the unretired genome with the fewest swarm runs (lowest id on a tie), so
    every live genome collects observations at the same rate. Seeds the table if empty."""
    seed_default(conn)
    row = conn.execute(
        """SELECT g.id, g.genome_json,
                  (SELECT COUNT(*) FROM swarm_runs r WHERE r.genome_id = g.id) AS n
           FROM swarm_genomes g WHERE g.retired_at IS NULL
           ORDER BY n ASC, g.id ASC LIMIT 1"""
    ).fetchone()
    if row is None:
        raise RuntimeError("every swarm genome is retired; add one or un-retire (swarm_genomes)")
    return _genome_row(row)


def live_genomes(conn: sqlite3.Connection) -> list[Genome]:
    rows = conn.execute(
        "SELECT id, genome_json FROM swarm_genomes WHERE retired_at IS NULL ORDER BY id"
    ).fetchall()
    return [_genome_row(r) for r in rows]


def retire_genome(conn: sqlite3.Connection, genome_id: int, reason: str) -> None:
    conn.execute(
        "UPDATE swarm_genomes SET retired_at = ?, retired_reason = ? "
        "WHERE id = ? AND retired_at IS NULL",
        (_now(), reason, genome_id),
    )
    conn.commit()


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


# ---------------------------------------------------------------------------
# Phase two: fitness
# ---------------------------------------------------------------------------

KPIS = ("impressions", "likes", "reposts", "replies", "quotes", "bookmarks")


@dataclass
class HeadMetric:
    """A posted swarm run: the thread's first tweet and its latest metric snapshot."""

    run_id: int
    draft_id: int
    genome_id: int | None
    winner: str | None
    tweet_id: str
    posted_at: str
    captured_on: str
    metrics: dict[str, int]


def fetch_head_metrics(conn: sqlite3.Connection) -> list[HeadMetric]:
    """Every swarm run whose draft was posted and has at least one metrics snapshot: the
    head tweet (position 1, status 'posted') with its newest non-deleted snapshot. Read-only
    on step 3's `posts` and step 4's `tweet_metrics`; [] when either is missing."""
    if not {"posts", "tweet_metrics"} <= _tables(conn):
        return []
    rows = conn.execute(
        """SELECT r.id AS run_id, r.draft_id, r.genome_id, r.winner,
                  p.tweet_id, p.posted_at,
                  m.captured_on, m.impressions, m.likes, m.reposts, m.replies, m.quotes,
                  m.bookmarks
           FROM swarm_runs r
           JOIN posts p ON p.draft_id = r.draft_id AND p.position = 1
                        AND p.status = 'posted' AND p.tweet_id IS NOT NULL
                        AND p.posted_at IS NOT NULL
           JOIN tweet_metrics m ON m.id = (
                SELECT id FROM tweet_metrics
                WHERE tweet_id = p.tweet_id AND deleted = 0
                ORDER BY captured_on DESC, id DESC LIMIT 1)
           WHERE r.draft_id IS NOT NULL
           ORDER BY p.posted_at, r.id"""
    ).fetchall()
    out: list[HeadMetric] = []
    for r in rows:
        out.append(
            HeadMetric(
                run_id=int(r[0]),
                draft_id=int(r[1]),
                genome_id=int(r[2]) if r[2] is not None else None,
                winner=r[3],
                tweet_id=str(r[4]),
                posted_at=str(r[5]),
                captured_on=str(r[6]),
                metrics={k: int(v or 0) for k, v in zip(KPIS, r[7:13], strict=True)},
            )
        )
    return out


def upsert_fitness(
    conn: sqlite3.Connection,
    *,
    run_id: int,
    draft_id: int,
    genome_id: int | None,
    winner: str | None,
    tweet_id: str,
    posted_at: str,
    kpi: str,
    value: float,
    baseline: float | None,
    relative: float | None,
) -> None:
    conn.execute(
        """INSERT INTO swarm_fitness (run_id, draft_id, genome_id, winner, tweet_id, posted_at,
                                      kpi, value, baseline, relative, computed_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(run_id) DO UPDATE SET
               genome_id = excluded.genome_id, winner = excluded.winner,
               tweet_id = excluded.tweet_id, posted_at = excluded.posted_at,
               kpi = excluded.kpi, value = excluded.value, baseline = excluded.baseline,
               relative = excluded.relative, computed_at = excluded.computed_at""",
        (
            run_id,
            draft_id,
            genome_id,
            winner,
            tweet_id,
            posted_at,
            kpi,
            value,
            baseline,
            relative,
            _now(),
        ),
    )
    conn.commit()


@dataclass
class FitnessRow:
    run_id: int
    draft_id: int
    genome_id: int | None
    winner: str | None
    posted_at: str
    kpi: str
    value: float
    baseline: float | None
    relative: float | None


def list_fitness(conn: sqlite3.Connection) -> list[FitnessRow]:
    rows = conn.execute(
        "SELECT run_id, draft_id, genome_id, winner, posted_at, kpi, value, baseline, relative "
        "FROM swarm_fitness ORDER BY posted_at, run_id"
    ).fetchall()
    return [FitnessRow(*r) for r in rows]


def genome_names(conn: sqlite3.Connection) -> dict[int, str]:
    return {int(r[0]): str(r[1]) for r in conn.execute("SELECT id, name FROM swarm_genomes")}
