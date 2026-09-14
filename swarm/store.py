"""Step 9's own tables on the pipeline database. It reads no other step's table.

swarm_genomes   the heritable part (swarm/genome.py), one row per genome; phase three adds
                children with parent_id, phase two retires losers (retired_at)
swarm_runs      one row per story the swarm was tried on: which genome, which variant won,
                how many model calls, and the full cell/tournament log
swarm_variants  the swarm and control drafts of a run as JSON, whether each passed the hard
                rules, so phase two can score the jury against X
swarm_fitness   phase two: one row per posted run, the head tweet's KPI, the trailing
                baseline and the relative score (run_evolve.py); phase three adds the
                designer credited alongside the writer genome
swarm_genomes.kind (phase three, guarded migration) is 'writer' or 'designer'; a designer
row's genome_json is swarm.genome.Designer. swarm_runs.designer_id records which one drew
the picture.

The two places step 9 reads another step's tables, both read-only and empty when a table is
missing: fetch_head_metrics (step 3's posts + step 4's tweet_metrics) and
fetch_winning_threads (step 2's drafts.thread_json, for the mutation prompt).
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime

from draft.schema import Draft
from swarm.genome import SEED_DESIGNERS, SEED_GENOMES, Designer, Genome

ROLES = ("swarm", "control")
KINDS = ("writer", "designer")


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
    # guarded migrations (phase two and three columns on phase-one tables)
    if "retired_reason" not in _columns(conn, "swarm_genomes"):
        conn.execute("ALTER TABLE swarm_genomes ADD COLUMN retired_reason TEXT")
    if "kind" not in _columns(conn, "swarm_genomes"):
        conn.execute("ALTER TABLE swarm_genomes ADD COLUMN kind TEXT NOT NULL DEFAULT 'writer'")
    if "designer_id" not in _columns(conn, "swarm_runs"):
        conn.execute("ALTER TABLE swarm_runs ADD COLUMN designer_id INTEGER")
    if "designer_id" not in _columns(conn, "swarm_fitness"):
        conn.execute("ALTER TABLE swarm_fitness ADD COLUMN designer_id INTEGER")
    conn.commit()


def insert_genome(
    conn: sqlite3.Connection, obj: Genome | Designer, *, kind: str | None = None
) -> int:
    """Store a writer genome or a designer; returns its id and sets obj.id."""
    kind = kind or ("designer" if isinstance(obj, Designer) else "writer")
    if kind not in KINDS:
        raise ValueError(f"unknown kind {kind!r}")
    cur = conn.execute(
        "INSERT INTO swarm_genomes (name, genome_json, parent_id, created_at, kind) "
        "VALUES (?, ?, ?, ?, ?)",
        (obj.name, obj.to_json(), obj.parent_id, _now(), kind),
    )
    conn.commit()
    obj.id = int(cur.lastrowid)
    return obj.id


def seed_default(conn: sqlite3.Connection) -> int:
    """Insert every seed writer genome and designer whose name is not yet in the table (a
    retired seed is not re-added). Returns the id of the default writer genome."""
    names = {
        (r[0], r[2]): int(r[1]) for r in conn.execute("SELECT name, id, kind FROM swarm_genomes")
    }
    for g in SEED_GENOMES:
        if (g.name, "writer") not in names:
            names[(g.name, "writer")] = insert_genome(conn, g, kind="writer")
    for d in SEED_DESIGNERS:
        if (d.name, "designer") not in names:
            names[(d.name, "designer")] = insert_genome(conn, d, kind="designer")
    return names[(SEED_GENOMES[0].name, "writer")]


def _genome_row(row: sqlite3.Row | tuple) -> Genome:
    return Genome.from_json(row[1], id=int(row[0]))


def active_genome(conn: sqlite3.Connection) -> Genome:
    """The newest unretired genome (seeded if none)."""
    seed_default(conn)
    row = conn.execute(
        "SELECT id, genome_json FROM swarm_genomes WHERE retired_at IS NULL "
        "AND kind = 'writer' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    return _genome_row(row)


def _next_row(conn: sqlite3.Connection, kind: str, run_column: str) -> sqlite3.Row | tuple:
    seed_default(conn)
    row = conn.execute(
        f"""SELECT g.id, g.genome_json,
                   (SELECT COUNT(*) FROM swarm_runs r WHERE r.{run_column} = g.id) AS n
            FROM swarm_genomes g WHERE g.retired_at IS NULL AND g.kind = ?
            ORDER BY n ASC, g.id ASC LIMIT 1""",
        (kind,),
    ).fetchone()
    if row is None:
        raise RuntimeError(f"every swarm {kind} is retired; add one or un-retire (swarm_genomes)")
    return row


def next_genome(conn: sqlite3.Connection) -> Genome:
    """Round-robin: the unretired writer genome with the fewest swarm runs (lowest id on a
    tie), so every live genome collects observations at the same rate. Seeds if empty."""
    return _genome_row(_next_row(conn, "writer", "genome_id"))


def next_designer(conn: sqlite3.Connection) -> Designer:
    """Round-robin over the live designers, by runs they drew the picture for."""
    row = _next_row(conn, "designer", "designer_id")
    return Designer.from_json(row[1], id=int(row[0]))


def live_genomes(conn: sqlite3.Connection, kind: str = "writer") -> list[Genome | Designer]:
    rows = conn.execute(
        "SELECT id, genome_json FROM swarm_genomes WHERE retired_at IS NULL AND kind = ? "
        "ORDER BY id",
        (kind,),
    ).fetchall()
    if kind == "designer":
        return [Designer.from_json(r[1], id=int(r[0])) for r in rows]
    return [_genome_row(r) for r in rows]


def get_genome(conn: sqlite3.Connection, genome_id: int) -> Genome | Designer | None:
    row = conn.execute(
        "SELECT id, genome_json, kind FROM swarm_genomes WHERE id = ?", (genome_id,)
    ).fetchone()
    if row is None:
        return None
    if row[2] == "designer":
        return Designer.from_json(row[1], id=int(row[0]))
    return _genome_row(row)


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
    designer_id: int | None = None,
) -> int:
    cur = conn.execute(
        """INSERT INTO swarm_runs (draft_id, item_id, cluster_id, genome_id, winner, calls,
                                   log_json, created_at, designer_id)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            draft_id,
            item_id,
            cluster_id,
            genome_id,
            winner,
            calls,
            json.dumps(log, ensure_ascii=False) if log is not None else None,
            _now(),
            designer_id,
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
    designer_id: int | None = None


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
                  m.bookmarks, r.designer_id
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
                designer_id=int(r[13]) if r[13] is not None else None,
            )
        )
    return out


def fetch_winning_threads(
    conn: sqlite3.Connection, genome_id: int, limit: int = 3
) -> list[tuple[float, list[str]]]:
    """The genome's best-scoring posted threads, (relative, thread) best first, from step 2's
    drafts.thread_json. Read-only; [] when drafts is missing or nothing is scored yet."""
    if "drafts" not in _tables(conn):
        return []
    rows = conn.execute(
        """SELECT f.relative, d.thread_json FROM swarm_fitness f
           JOIN drafts d ON d.id = f.draft_id
           WHERE f.genome_id = ? AND f.relative IS NOT NULL
           ORDER BY f.relative DESC, f.run_id DESC LIMIT ?""",
        (genome_id, limit),
    ).fetchall()
    out = []
    for rel, thread_json in rows:
        try:
            thread = json.loads(thread_json or "[]")
        except ValueError:
            continue
        if isinstance(thread, list) and thread:
            out.append((float(rel), [str(p) for p in thread]))
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
    designer_id: int | None = None,
) -> None:
    conn.execute(
        """INSERT INTO swarm_fitness (run_id, draft_id, genome_id, winner, tweet_id, posted_at,
                                      kpi, value, baseline, relative, computed_at, designer_id)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(run_id) DO UPDATE SET
               genome_id = excluded.genome_id, winner = excluded.winner,
               tweet_id = excluded.tweet_id, posted_at = excluded.posted_at,
               kpi = excluded.kpi, value = excluded.value, baseline = excluded.baseline,
               relative = excluded.relative, computed_at = excluded.computed_at,
               designer_id = excluded.designer_id""",
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
            designer_id,
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
    designer_id: int | None = None


def list_fitness(conn: sqlite3.Connection) -> list[FitnessRow]:
    rows = conn.execute(
        "SELECT run_id, draft_id, genome_id, winner, posted_at, kpi, value, baseline, relative, "
        "designer_id FROM swarm_fitness ORDER BY posted_at, run_id"
    ).fetchall()
    return [FitnessRow(*r) for r in rows]


def genome_names(conn: sqlite3.Connection) -> dict[int, str]:
    return {int(r[0]): str(r[1]) for r in conn.execute("SELECT id, name FROM swarm_genomes")}


def all_names(conn: sqlite3.Connection, kind: str) -> set[str]:
    rows = conn.execute("SELECT name FROM swarm_genomes WHERE kind = ?", (kind,))
    return {str(r[0]) for r in rows}
