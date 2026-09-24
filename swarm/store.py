"""Step 9's own tables on the pipeline database. It reads no other step's table.

swarm_genomes   the heritable part (swarm/genome.py), one row per genome; phase three adds
                children with parent_id, phase two retires losers (retired_at)
swarm_runs      one row per story the swarm was tried on: which genome, which variant won,
                how many model calls, and the full cell/tournament log
swarm_variants  the swarm and control drafts of a run as JSON, whether each passed the hard
                rules, so phase two can score the jury against X
swarm_fitness   phase two: one row per posted run, the head tweet's KPI, the trailing
                baseline and the relative score (run_evolve.py); phase three adds the
                designer credited alongside the writer genome. genome_id and designer_id
                here are the genomes CREDITED with the post, NULL when that genome did not
                shape it (the control's text was posted, a single post never ran the
                writer's slot rules, the chart was not drawn in the designer's Style);
                swarm_runs keeps what was drawn, and swarm_runs.styled whether
                run_draft drew the chart in the designer's Style
swarm_genomes.kind (phase three, guarded migration) is 'writer' or 'designer'; a designer
row's genome_json is swarm.genome.Designer. swarm_runs.designer_id records which one drew
the picture.

The two places step 9 reads another step's tables, both read-only and empty when a table is
missing: fetch_head_metrics (step 3's posts + step 4's tweet_metrics) and
fetch_winning_threads (step 2's drafts.thread_json, for the mutation prompt). It also
imports feedback.models for the KPI weights only (pure dataclasses, no DB and no network),
so the fitness loop and the feedback report weight `conversation` identically.
"""

from __future__ import annotations

import json
import random
import re
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from statistics import median
from typing import Any

from draft.schema import Draft
from feedback import models as feedback_models
from swarm import fitness
from swarm.genome import (
    CLOSER,
    CLOSER_RULE,
    NAME_THE_SOURCE,
    SEED_DESIGNERS,
    SEED_FORMATS,
    SEED_GENOMES,
    Designer,
    FormatGenome,
    Genome,
    asks_for_url,
)

ROLES = ("swarm", "control")
KINDS = ("writer", "designer", "format")


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
    if "format_id" not in _columns(conn, "swarm_runs"):
        conn.execute("ALTER TABLE swarm_runs ADD COLUMN format_id INTEGER")
    if "format_id" not in _columns(conn, "swarm_fitness"):
        conn.execute("ALTER TABLE swarm_fitness ADD COLUMN format_id INTEGER")
    if "styled" not in _columns(conn, "swarm_runs"):
        # 1 once run_draft drew a chart in the run's designer Style; 0 when it did not (a
        # table, a failed render, no picture); NULL for runs from before this was tracked
        conn.execute("ALTER TABLE swarm_runs ADD COLUMN styled INTEGER")
    _rewrite_url_closers(conn)
    conn.commit()


# The seed closer rule before rule 2; genomes seeded or bred from it carry it word for word.
LEGACY_CLOSER_RULE = (
    "Close in one sentence with the one-line takeaway and then the primary source URL "
    "verbatim on its own. If the source is a preprint, say so here too."
)


# The URL clause the old seed closer carried, as children that kept it still word it.
_URL_CLAUSE = re.compile(
    r",?\s*(?:and\s+)?then\s+(?:the\s+)?(?:primary\s+)?(?:source\s+)?url\b[^.;]*",
    re.IGNORECASE,
)


def closer_without_url(rule: str) -> str:
    """A closer rule with its request for the source URL taken out and the source named in
    words instead, keeping the rest of a child's own wording (so a reworded closer never
    collapses into its parent's). The exact old seed rule becomes CLOSER_RULE; a rule with
    nothing left becomes CLOSER_RULE too."""
    norm = " ".join(rule.split())
    if norm == LEGACY_CLOSER_RULE:
        return CLOSER_RULE
    kept = _URL_CLAUSE.sub("", norm)
    sentences = [x for x in re.split(r"(?<=[.!?])\s+", kept) if x.strip()]
    sentences = [x for x in sentences if not asks_for_url(x)]
    if not sentences:
        return CLOSER_RULE
    text = " ".join(x if x.rstrip().endswith((".", "!", "?")) else x + "." for x in sentences)
    return text if NAME_THE_SOURCE in text else f"{text} {NAME_THE_SOURCE}"


def _rewrite_url_closers(conn: sqlite3.Connection) -> None:
    """Rule 2 bans every link, but writer genomes seeded or bred before it still tell their
    closer to end on the source URL, a job no candidate may do. Rewrite that closer in
    place on live writers (same id, so rotation counts and fitness stay), noting the old
    rule: the exact old seed rule becomes CLOSER_RULE, a child's reworded closer loses only
    its URL request (closer_without_url). Idempotent: a closer that asks for no URL is left
    alone."""
    rows = conn.execute(
        "SELECT id, genome_json FROM swarm_genomes WHERE retired_at IS NULL AND kind = 'writer'"
    ).fetchall()
    for gid, text in rows:
        try:
            d = json.loads(text)
        except ValueError:
            continue
        slots = d.get("slots") or []
        changed = False
        for slot in slots:
            if (
                isinstance(slot, dict)
                and slot.get("name") == CLOSER
                and asks_for_url(str(slot.get("rule", "")))
            ):
                old = " ".join(str(slot.get("rule", "")).split())
                slot["rule"] = closer_without_url(old)
                changed = True
        if changed:
            note = f"closer rule rewritten without the source URL (rule 2); was: {old}"
            d["notes"] = f"{d.get('notes', '')} {note}".strip()
            conn.execute(
                "UPDATE swarm_genomes SET genome_json = ? WHERE id = ?",
                (json.dumps(d, ensure_ascii=False), gid),
            )


def _kind_of(obj: Genome | Designer | FormatGenome) -> str:
    if isinstance(obj, Designer):
        return "designer"
    if isinstance(obj, FormatGenome):
        return "format"
    return "writer"


def insert_genome(
    conn: sqlite3.Connection, obj: Genome | Designer | FormatGenome, *, kind: str | None = None
) -> int:
    """Store a writer genome, a designer or a format; returns its id and sets obj.id."""
    kind = kind or _kind_of(obj)
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
    for f in SEED_FORMATS:
        if (f.name, "format") not in names:
            names[(f.name, "format")] = insert_genome(conn, f, kind="format")
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


def _next_row(
    conn: sqlite3.Connection,
    kind: str,
    run_column: str,
    writer_id: int | None = None,
    format_id: int | None = None,
) -> sqlite3.Row | tuple:
    """The live row of `kind` with the fewest runs since the newest live row of that kind
    was born (lowest id on a tie). Counting from the last birth restarts an even rotation
    each time a child joins, instead of handing the child every story until it has caught
    up with incumbents' lifetime counts. With `format_id` and/or `writer_id`, runs paired
    with that format, then with that writer, are counted first, so every designer meets
    every format and writer in turn instead of the same fixed few (rotations of sizes
    that share a factor otherwise lock into step)."""
    seed_default(conn)

    def paired(column: str, key: str, value: int | None) -> str:
        if value is None:
            return "0"
        return (
            f"(SELECT COUNT(*) FROM swarm_runs r WHERE r.{run_column} = g.id "
            f"AND r.{column} = :{key} AND r.created_at >= :since)"
        )

    row = conn.execute(
        f"""SELECT g.id, g.genome_json,
                   {paired("format_id", "format", format_id)} AS by_format,
                   {paired("genome_id", "writer", writer_id)} AS by_writer,
                   (SELECT COUNT(*) FROM swarm_runs r WHERE r.{run_column} = g.id
                      AND r.created_at >= :since) AS n
            FROM swarm_genomes g WHERE g.retired_at IS NULL AND g.kind = :kind
            ORDER BY by_format ASC, by_writer ASC, n ASC, g.id ASC LIMIT 1""",
        {
            "kind": kind,
            "writer": writer_id,
            "format": format_id,
            "since": _newest_birth(conn, kind),
        },
    ).fetchone()
    if row is None:
        raise RuntimeError(f"every swarm {kind} is retired; add one or un-retire (swarm_genomes)")
    return row


def _newest_birth(conn: sqlite3.Connection, kind: str) -> str:
    row = conn.execute(
        "SELECT MAX(created_at) FROM swarm_genomes WHERE retired_at IS NULL AND kind = ?",
        (kind,),
    ).fetchone()
    return str(row[0] or "")


def next_genome(conn: sqlite3.Connection) -> Genome:
    """Round-robin: the unretired writer genome with the fewest swarm runs since the newest
    live writer was born (lowest id on a tie), so every live genome collects observations
    at the same rate, a newborn included. Seeds if empty."""
    return _genome_row(_next_row(conn, "writer", "genome_id"))


def next_designer(
    conn: sqlite3.Connection, writer_id: int | None = None, format_id: int | None = None
) -> Designer:
    """Round-robin over the live designers, by runs they drew the picture for; with
    `format_id` / `writer_id`, the designer paired least with that format, then with that
    writer, goes first (draw the format first)."""
    row = _next_row(conn, "designer", "designer_id", writer_id, format_id)
    return Designer.from_json(row[1], id=int(row[0]))


def next_format(conn: sqlite3.Connection, writer_id: int | None = None) -> FormatGenome:
    """Round-robin over the live format genomes (phase four); with `writer_id`, the format
    this writer has been paired with least goes first."""
    row = _next_row(conn, "format", "format_id", writer_id)
    return FormatGenome.from_json(row[1], id=int(row[0]))


# swarm_fitness column that credits each kind
FITNESS_COLUMN = {"writer": "genome_id", "designer": "designer_id", "format": "format_id"}


def fitness_scores(conn: sqlite3.Connection, kind: str) -> dict[int, fitness.GenomeScore]:
    """Every genome of `kind` (live or retired) with its credited, scored posts in
    swarm_fitness, as fitness.GenomeScore. {} when the table is missing or empty."""
    if "swarm_fitness" not in _tables(conn):
        return {}
    col = FITNESS_COLUMN[kind]
    by: dict[int, list[float]] = {}
    for gid, rel in conn.execute(
        f"SELECT {col}, relative FROM swarm_fitness "
        f"WHERE {col} IS NOT NULL AND relative IS NOT NULL ORDER BY run_id"
    ).fetchall():
        by.setdefault(int(gid), []).append(float(rel))
    return {g: fitness.GenomeScore(g, len(v), float(median(v)), v) for g, v in by.items()}


def dead_runs(
    conn: sqlite3.Connection, kind: str, *, now: datetime, dead_after_days: float
) -> dict[int, int]:
    """Per genome of `kind`, how many of its drafted runs can no longer be credited to it: at
    once for a writer whose swarm text was not posted (the jury chose the control, or the
    swarm failed) or whose format was a single post, and for a designer whose chart was not
    drawn in its Style; for any kind, a run older than `dead_after_days` with no credited
    swarm_fitness row (never approved, never posted, or deleted). Only runs with a draft
    count (a run whose drafting crashed never reached a human)."""
    col = FITNESS_COLUMN[kind]
    cutoff = (now - timedelta(days=dead_after_days)).isoformat()
    at_once = "0"
    if kind == "writer":
        singles = [
            int(i)
            for i, text in conn.execute(
                "SELECT id, genome_json FROM swarm_genomes WHERE kind = 'format'"
            ).fetchall()
            if _format_fields(text)[0] == "single"
        ]
        in_single = f"r.format_id IN ({','.join(str(i) for i in singles)})" if singles else "0"
        at_once = f"(r.winner IS NOT 'swarm' OR {in_single})"
    elif kind == "designer":
        at_once = "(r.styled = 0)"
    has_fitness = "swarm_fitness" in _tables(conn)
    credited = (
        f"EXISTS (SELECT 1 FROM swarm_fitness f WHERE f.run_id = r.id AND f.{col} = r.{col})"
        if has_fitness
        else "0"
    )
    rows = conn.execute(
        f"""SELECT r.{col}, COUNT(*) FROM swarm_runs r
            WHERE r.{col} IS NOT NULL AND r.draft_id IS NOT NULL
              AND ({at_once} OR (r.created_at < :cutoff AND NOT {credited}))
            GROUP BY r.{col}""",
        {"cutoff": cutoff},
    ).fetchall()
    return {int(g): int(n) for g, n in rows}


def inherited_priors(
    conn: sqlite3.Connection,
    kind: str,
    scores: dict[int, fitness.GenomeScore],
    *,
    prior_sd: float,
    post_sd: float,
    dead: dict[int, int] | None = None,
    dead_half_life: float = 0.0,
) -> dict[int, float]:
    """Each genome's starting belief: its parent's posterior, where the parent's own prior
    is in turn its parent's (a grandchild of a strong line starts strong, not at 0), halved
    for every `dead_half_life` of the genome's own runs that could never be credited, so a
    child whose swarm keeps losing the jury drifts back to the account's average instead of
    living off its parent's record. 0 for a seed."""
    parent_of = {
        int(i): (int(p) if p is not None else None)
        for i, p in conn.execute(
            "SELECT id, parent_id FROM swarm_genomes WHERE kind = ?", (kind,)
        ).fetchall()
    }
    memo: dict[int, float] = {}

    def prior(gid: int, seen: frozenset[int]) -> float:
        if gid in memo:
            return memo[gid]
        pid = parent_of.get(gid)
        if pid is None or gid in seen:
            return 0.0
        base = fitness.posterior(
            scores.get(pid),
            prior_mean=prior(pid, seen | {gid}),
            prior_sd=prior_sd,
            post_sd=post_sd,
        )[0]
        if dead and dead_half_life > 0:
            base *= 0.5 ** (dead.get(gid, 0) / dead_half_life)
        memo[gid] = base
        return base

    return {gid: prior(gid, frozenset()) for gid in parent_of}


def thompson_next(
    conn: sqlite3.Connection,
    kind: str,
    *,
    rng: random.Random,
    prior_sd: float,
    post_sd: float,
    min_sd: float,
    explore_floor: float = 0.0,
    now: datetime | None = None,
    dead_after_days: float = 7.0,
    dead_half_life: float = 3.0,
) -> Genome | Designer | FormatGenome | None:
    """A live genome of `kind` drawn by Thompson sampling on its credited scores
    (fitness.thompson_pick): the better a genome has done, the more stories it drafts,
    while one with little evidence still gets some. A genome starts from its parent's
    posterior (inherited_priors), so a winner's offspring is tried straight away, and that
    head start wears off with every run that could never be credited to it (dead_runs).
    With probability `explore_floor` the draw goes to the rotation's next genome instead
    (fewest runs since the last birth), so a genome the leader has crowded out is still
    judged in time. None while no live genome of the kind has a scored post: then the
    caller rotates evenly."""
    seed_default(conn)
    live = live_genomes(conn, kind)
    if not live:
        raise RuntimeError(f"every swarm {kind} is retired; add one or un-retire (swarm_genomes)")
    scores = fitness_scores(conn, kind)
    if not any(g.id in scores for g in live):
        return None
    if explore_floor > 0 and rng.random() < explore_floor:
        return _parse(kind, *_rotation_row(conn, kind))
    spread = fitness.allocation_spread(scores.values(), default=post_sd, floor=min_sd)
    dead = dead_runs(conn, kind, now=now or datetime.now(UTC), dead_after_days=dead_after_days)
    prior_means = inherited_priors(
        conn,
        kind,
        scores,
        prior_sd=prior_sd,
        post_sd=spread,
        dead=dead,
        dead_half_life=dead_half_life,
    )
    pick = fitness.thompson_pick(
        [g.id for g in live],
        scores,
        rng=rng,
        prior_sd=prior_sd,
        post_sd=spread,
        prior_means=prior_means,
    )
    return next(g for g in live if g.id == pick)


def _rotation_row(conn: sqlite3.Connection, kind: str) -> tuple[str, int]:
    row = _next_row(conn, kind, FITNESS_COLUMN[kind])
    return str(row[1]), int(row[0])


def _parse(kind: str, text: str, id: int) -> Genome | Designer | FormatGenome:
    if kind == "designer":
        return Designer.from_json(text, id=id)
    if kind == "format":
        return FormatGenome.from_json(text, id=id)
    return Genome.from_json(text, id=id)


def live_genomes(
    conn: sqlite3.Connection, kind: str = "writer"
) -> list[Genome | Designer | FormatGenome]:
    rows = conn.execute(
        "SELECT id, genome_json FROM swarm_genomes WHERE retired_at IS NULL AND kind = ? "
        "ORDER BY id",
        (kind,),
    ).fetchall()
    return [_parse(kind, r[1], int(r[0])) for r in rows]


def get_genome(conn: sqlite3.Connection, genome_id: int) -> Genome | Designer | FormatGenome | None:
    row = conn.execute(
        "SELECT id, genome_json, kind FROM swarm_genomes WHERE id = ?", (genome_id,)
    ).fetchone()
    if row is None:
        return None
    return _parse(row[2] or "writer", row[1], int(row[0]))


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
    format_id: int | None = None,
) -> int:
    cur = conn.execute(
        """INSERT INTO swarm_runs (draft_id, item_id, cluster_id, genome_id, winner, calls,
                                   log_json, created_at, designer_id, format_id, styled)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)""",
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
            format_id,
        ),
    )
    conn.commit()
    return int(cur.lastrowid)


def set_run_draft(conn: sqlite3.Connection, run_id: int, draft_id: int) -> None:
    conn.execute("UPDATE swarm_runs SET draft_id = ? WHERE id = ?", (draft_id, run_id))
    conn.commit()


def mark_styled(conn: sqlite3.Connection, run_id: int) -> None:
    """run_draft drew this run's chart in its designer's Style, so the designer may be
    credited with the post (fitness.designer_credited). A picture redrawn later in the
    queue (a revision, a table the verifier renders) is not tracked."""
    conn.execute("UPDATE swarm_runs SET styled = 1 WHERE id = ?", (run_id,))
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

# The six stored columns, in the order fetch_head_metrics selects them.
METRIC_COLUMNS = ("impressions", "likes", "reposts", "replies", "quotes", "bookmarks")
# What evolve.kpi may name: the stored columns plus the derived `conversation` weighting
# (feedback/models.py), which is what selection should point at.
KPIS = (*METRIC_COLUMNS, feedback_models.CONVERSATION)


def _metrics(values: Sequence[Any]) -> dict[str, int]:
    """The stored counts plus the derived `conversation` KPI, so a caller can read either
    by name. Computed through feedback/models.py so both loops weight it the same way."""
    counts = {k: int(v or 0) for k, v in zip(METRIC_COLUMNS, values, strict=True)}
    counts[feedback_models.CONVERSATION] = feedback_models.Metrics(**counts).conversation
    return counts


@dataclass
class HeadMetric:
    """A posted swarm run: the thread's first tweet and the metric snapshot it is scored on."""

    run_id: int
    draft_id: int
    genome_id: int | None
    winner: str | None
    tweet_id: str
    posted_at: str
    captured_on: str
    metrics: dict[str, int]
    designer_id: int | None = None
    format_id: int | None = None
    shape: str | None = None  # the run's format shape; None before phase four
    visuals: int | None = None  # pictures the run's format asked for
    captured_at: str = ""
    own_replies: int = 0  # the account's own post 2, a reply to this head (0 or 1)
    styled: bool | None = None  # a chart was drawn in the designer's Style (None: unknown)


def _when(text: str) -> datetime:
    """An ISO timestamp as an aware UTC datetime (a naive one is taken as UTC)."""
    dt = datetime.fromisoformat(str(text).replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def _format_fields(genome_json: str | None) -> tuple[str | None, int | None]:
    if not genome_json:
        return None, None
    try:
        d = json.loads(genome_json)
        return str(d.get("shape", "thread")), int(d.get("visuals", 1))
    except (ValueError, TypeError, AttributeError):
        return None, None


def pick_snapshot(snapshots: Sequence[Any], posted_at: str, horizon_hours: float) -> Any | None:
    """The snapshot a head is scored on. With horizon_hours <= 0, the newest one. Otherwise
    the EARLIEST one taken at least horizon_hours after posting, so every post is compared
    at about the same age; None while the post is younger than that (it is not scored yet,
    and it is nobody's baseline either). `snapshots` are oldest first and carry
    captured_at."""
    if not snapshots:
        return None
    if horizon_hours <= 0:
        return snapshots[-1]
    posted = _when(posted_at)
    for snap in snapshots:
        if (_when(snap["captured_at"]) - posted).total_seconds() >= horizon_hours * 3600:
            return snap
    return None


def fetch_head_metrics(
    conn: sqlite3.Connection,
    *,
    horizon_hours: float = 0.0,
    subtract_self_reply: bool = False,
) -> list[HeadMetric]:
    """Every swarm run whose draft was posted and has a metrics snapshot to be scored on: the
    head tweet (position 1, status 'posted') with the snapshot `pick_snapshot` chooses
    among its non-deleted ones (`horizon_hours` 0: the newest, as before). With
    `subtract_self_reply`, the head's reply count loses the account's own post 2, which X
    counts as a reply to the head, so a thread does not out-score a single post by one
    reply it wrote itself. Each head also carries its format's shape and picture count
    (swarm_genomes via swarm_runs.format_id, this step's own tables) for crediting.
    Read-only on step 3's `posts` and step 4's `tweet_metrics`; [] when either is
    missing."""
    if not {"posts", "tweet_metrics"} <= _tables(conn):
        return []
    rows = conn.execute(
        """SELECT r.id AS run_id, r.draft_id, r.genome_id, r.winner,
                  p.tweet_id, p.posted_at, r.designer_id, r.format_id,
                  fg.genome_json AS format_json, r.styled,
                  (SELECT COUNT(*) FROM posts q
                    WHERE q.draft_id = r.draft_id AND q.position = 2
                      AND q.status = 'posted' AND q.tweet_id IS NOT NULL) AS own_replies
           FROM swarm_runs r
           JOIN posts p ON p.draft_id = r.draft_id AND p.position = 1
                        AND p.status = 'posted' AND p.tweet_id IS NOT NULL
                        AND p.posted_at IS NOT NULL
           LEFT JOIN swarm_genomes fg ON fg.id = r.format_id
           WHERE r.draft_id IS NOT NULL
           ORDER BY p.posted_at, r.id"""
    ).fetchall()
    if not rows:
        return []
    snaps: dict[str, list[dict[str, Any]]] = {}
    for m in conn.execute(
        """SELECT m.tweet_id, m.captured_on, m.captured_at, m.impressions, m.likes, m.reposts,
                  m.replies, m.quotes, m.bookmarks
           FROM tweet_metrics m
           WHERE m.deleted = 0 AND m.tweet_id IN (
                SELECT tweet_id FROM posts WHERE position = 1 AND status = 'posted')
           ORDER BY m.tweet_id, m.captured_on, m.id"""
    ).fetchall():
        snaps.setdefault(str(m[0]), []).append(
            {"captured_on": str(m[1]), "captured_at": str(m[2]), "values": tuple(m[3:9])}
        )
    out: list[HeadMetric] = []
    for r in rows:
        snap = pick_snapshot(snaps.get(str(r[4]), []), str(r[5]), horizon_hours)
        if snap is None:
            continue
        values = list(snap["values"])
        own = int(r[10] or 0)
        if subtract_self_reply and own:
            i = METRIC_COLUMNS.index("replies")
            values[i] = max(0, int(values[i] or 0) - own)
        shape, visuals = _format_fields(r[8])
        out.append(
            HeadMetric(
                run_id=int(r[0]),
                draft_id=int(r[1]),
                genome_id=int(r[2]) if r[2] is not None else None,
                winner=r[3],
                tweet_id=str(r[4]),
                posted_at=str(r[5]),
                captured_on=snap["captured_on"],
                metrics=_metrics(values),
                designer_id=int(r[6]) if r[6] is not None else None,
                format_id=int(r[7]) if r[7] is not None else None,
                shape=shape,
                visuals=visuals,
                captured_at=snap["captured_at"],
                own_replies=own,
                styled=None if r[9] is None else bool(r[9]),
            )
        )
    return out


def fetch_winning_threads(
    conn: sqlite3.Connection, genome_id: int, limit: int = 3
) -> list[tuple[float, list[str]]]:
    """The genome's best-scoring posted threads, (relative, thread) best first, from step 2's
    drafts.thread_json. Only posts the genome is credited with (swarm_fitness.genome_id),
    and only ones the jury gave to the swarm, so the breeder never studies the control's
    text as this genome's work. Read-only; [] when drafts is missing or nothing is scored
    yet."""
    if "drafts" not in _tables(conn):
        return []
    rows = conn.execute(
        """SELECT f.relative, d.thread_json FROM swarm_fitness f
           JOIN drafts d ON d.id = f.draft_id
           WHERE f.genome_id = ? AND f.relative IS NOT NULL AND f.winner = 'swarm'
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


def keep_fitness(conn: sqlite3.Connection, run_ids: Sequence[int]) -> int:
    """Delete swarm_fitness rows for runs a fresh score did not produce (a post not old
    enough for the horizon yet, a deleted tweet, a row written under older rules), so the
    table is always exactly what the current rules score. Returns the rows deleted."""
    keep = sorted({int(r) for r in run_ids})
    if not keep:
        cur = conn.execute("DELETE FROM swarm_fitness")
    else:
        conn.execute("CREATE TEMP TABLE IF NOT EXISTS _keep_runs (run_id INTEGER PRIMARY KEY)")
        conn.execute("DELETE FROM _keep_runs")
        conn.executemany("INSERT INTO _keep_runs (run_id) VALUES (?)", [(r,) for r in keep])
        cur = conn.execute(
            "DELETE FROM swarm_fitness WHERE run_id NOT IN (SELECT run_id FROM _keep_runs)"
        )
    conn.commit()
    return int(cur.rowcount or 0)


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
    format_id: int | None = None,
) -> None:
    conn.execute(
        """INSERT INTO swarm_fitness (run_id, draft_id, genome_id, winner, tweet_id, posted_at,
                                      kpi, value, baseline, relative, computed_at, designer_id,
                                      format_id)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(run_id) DO UPDATE SET
               genome_id = excluded.genome_id, winner = excluded.winner,
               tweet_id = excluded.tweet_id, posted_at = excluded.posted_at,
               kpi = excluded.kpi, value = excluded.value, baseline = excluded.baseline,
               relative = excluded.relative, computed_at = excluded.computed_at,
               designer_id = excluded.designer_id, format_id = excluded.format_id""",
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
            format_id,
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
    format_id: int | None = None


def list_fitness(conn: sqlite3.Connection) -> list[FitnessRow]:
    rows = conn.execute(
        "SELECT run_id, draft_id, genome_id, winner, posted_at, kpi, value, baseline, relative, "
        "designer_id, format_id FROM swarm_fitness ORDER BY posted_at, run_id"
    ).fetchall()
    return [FitnessRow(*r) for r in rows]


def genome_names(conn: sqlite3.Connection) -> dict[int, str]:
    return {int(r[0]): str(r[1]) for r in conn.execute("SELECT id, name FROM swarm_genomes")}


def all_names(conn: sqlite3.Connection, kind: str) -> set[str]:
    rows = conn.execute("SELECT name FROM swarm_genomes WHERE kind = ?", (kind,))
    return {str(r[0]) for r in rows}
