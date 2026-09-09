"""SQLite storage: items, clusters, scores, ratings, source_runs."""

from __future__ import annotations

import json
import logging
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from typing import Any

from pydantic import BaseModel

from ingest.base import Item, utcnow

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    id            TEXT PRIMARY KEY,
    source        TEXT NOT NULL,
    url           TEXT NOT NULL,
    doi           TEXT,
    title         TEXT NOT NULL,
    abstract      TEXT NOT NULL DEFAULT '',
    published_at  TEXT,
    fetched_at    TEXT NOT NULL,
    dedup_hash    TEXT NOT NULL UNIQUE,
    cluster_id    INTEGER REFERENCES clusters(id),
    raw_json      TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_items_doi ON items(doi);
CREATE INDEX IF NOT EXISTS idx_items_cluster ON items(cluster_id);

CREATE TABLE IF NOT EXISTS clusters (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    title            TEXT NOT NULL,          -- title of the first member
    norm_title       TEXT NOT NULL,          -- normalised title for near-dup matching
    doi              TEXT,
    published_at     TEXT,                   -- earliest member published_at
    created_at       TEXT NOT NULL,
    prefilter_status TEXT,                   -- NULL | 'pass' | 'drop'
    prefilter_reason TEXT,
    prefiltered_at   TEXT                    -- when status was last set (cap accounting)
);
CREATE INDEX IF NOT EXISTS idx_clusters_published ON clusters(published_at);
CREATE INDEX IF NOT EXISTS idx_clusters_doi ON clusters(doi);

CREATE TABLE IF NOT EXISTS scores (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    cluster_id            INTEGER NOT NULL REFERENCES clusters(id),
    model                 TEXT NOT NULL,
    prompt_version        TEXT NOT NULL,
    novelty               INTEGER,
    clinical_significance INTEGER,
    audience_interest     INTEGER,
    expertise_fit         INTEGER,
    timeliness            INTEGER,
    evidence_level        TEXT,
    hype_risk             INTEGER,
    total                 INTEGER,
    rationale             TEXT,
    suggested_angle       TEXT,
    raw_response          TEXT NOT NULL,
    scored_at             TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_scores_cluster ON scores(cluster_id);

CREATE TABLE IF NOT EXISTS ratings (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    cluster_id  INTEGER NOT NULL REFERENCES clusters(id),
    rating      INTEGER NOT NULL CHECK (rating BETWEEN 1 AND 5),
    note        TEXT,
    rated_at    TEXT NOT NULL,
    rater       TEXT                           -- NULL/'human', or 'auto:<model>'
);

CREATE TABLE IF NOT EXISTS source_runs (
    source       TEXT PRIMARY KEY,
    last_run_at  TEXT NOT NULL,
    fetched      INTEGER NOT NULL DEFAULT 0,
    inserted     INTEGER NOT NULL DEFAULT 0,
    error        TEXT
);
"""


def _iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC).isoformat()


def _parse(s: str | None) -> datetime | None:
    if not s:
        return None
    dt = datetime.fromisoformat(s)
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


class Cluster(BaseModel):
    id: int
    title: str
    norm_title: str
    doi: str | None = None
    published_at: datetime | None = None
    created_at: datetime
    prefilter_status: str | None = None
    prefilter_reason: str | None = None
    member_ids: list[str] = []
    sources: list[str] = []


class Score(BaseModel):
    id: int | None = None
    cluster_id: int
    model: str
    prompt_version: str
    novelty: int
    clinical_significance: int
    audience_interest: int
    expertise_fit: int
    timeliness: int
    evidence_level: str
    hype_risk: int
    total: int
    rationale: str
    suggested_angle: str
    raw_response: str
    scored_at: datetime


class Database:
    def __init__(self, path: str = ":memory:"):
        self.path = path
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.executescript(SCHEMA)
        self._migrate()

    # Additive, guarded migrations for databases created before a column existed.
    _MIGRATIONS = (
        ("clusters", "prefiltered_at", "ALTER TABLE clusters ADD COLUMN prefiltered_at TEXT"),
        ("ratings", "rater", "ALTER TABLE ratings ADD COLUMN rater TEXT"),
    )

    def _migrate(self) -> None:
        for table, column, ddl in self._MIGRATIONS:
            cols = {r[1] for r in self.conn.execute(f"PRAGMA table_info({table})").fetchall()}
            if column not in cols:
                self.conn.execute(ddl)
                self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        try:
            yield self.conn
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

    # ---- items -------------------------------------------------------------
    def item_exists(self, dedup_hash: str) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM items WHERE dedup_hash = ?", (dedup_hash,)
        ).fetchone()
        return row is not None

    def insert_item(self, item: Item) -> bool:
        """Insert; return False if dedup_hash already present."""
        try:
            with self.tx() as c:
                c.execute(
                    """INSERT INTO items (id, source, url, doi, title, abstract, published_at,
                       fetched_at, dedup_hash, cluster_id, raw_json)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        item.id,
                        item.source,
                        item.url,
                        item.doi,
                        item.title,
                        item.abstract,
                        _iso(item.published_at),
                        _iso(item.fetched_at),
                        item.dedup_hash,
                        item.cluster_id,
                        item.raw_json,
                    ),
                )
            return True
        except sqlite3.IntegrityError:
            return False

    def _row_to_item(self, r: sqlite3.Row) -> Item:
        return Item(
            id=r["id"],
            source=r["source"],
            url=r["url"],
            doi=r["doi"],
            title=r["title"],
            abstract=r["abstract"],
            published_at=_parse(r["published_at"]),
            fetched_at=_parse(r["fetched_at"]) or utcnow(),
            dedup_hash=r["dedup_hash"],
            cluster_id=r["cluster_id"],
            raw_json=r["raw_json"],
        )

    def get_item(self, item_id: str) -> Item | None:
        r = self.conn.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
        return self._row_to_item(r) if r else None

    def find_item_by_doi(self, doi: str) -> Item | None:
        r = self.conn.execute("SELECT * FROM items WHERE doi = ? LIMIT 1", (doi,)).fetchone()
        return self._row_to_item(r) if r else None

    def items_in_cluster(self, cluster_id: int) -> list[Item]:
        rows = self.conn.execute(
            "SELECT * FROM items WHERE cluster_id = ? ORDER BY published_at", (cluster_id,)
        ).fetchall()
        return [self._row_to_item(r) for r in rows]

    def set_item_cluster(self, item_id: str, cluster_id: int) -> None:
        with self.tx() as c:
            c.execute("UPDATE items SET cluster_id = ? WHERE id = ?", (cluster_id, item_id))

    # ---- clusters ----------------------------------------------------------
    def create_cluster(
        self, title: str, norm_title: str, doi: str | None, published_at: datetime | None
    ) -> int:
        with self.tx() as c:
            cur = c.execute(
                "INSERT INTO clusters (title, norm_title, doi, published_at, created_at) "
                "VALUES (?,?,?,?,?)",
                (title, norm_title, doi, _iso(published_at), _iso(utcnow())),
            )
            return int(cur.lastrowid)

    def _row_to_cluster(self, r: sqlite3.Row) -> Cluster:
        members = self.conn.execute(
            "SELECT id, source FROM items WHERE cluster_id = ? ORDER BY published_at", (r["id"],)
        ).fetchall()
        ids = [x["id"] for x in members]
        sources = sorted({x["source"] for x in members})
        return Cluster(
            id=r["id"],
            title=r["title"],
            norm_title=r["norm_title"],
            doi=r["doi"],
            published_at=_parse(r["published_at"]),
            created_at=_parse(r["created_at"]),
            prefilter_status=r["prefilter_status"],
            prefilter_reason=r["prefilter_reason"],
            member_ids=ids,
            sources=sources,
        )

    def get_cluster(self, cluster_id: int) -> Cluster | None:
        r = self.conn.execute("SELECT * FROM clusters WHERE id = ?", (cluster_id,)).fetchone()
        return self._row_to_cluster(r) if r else None

    def find_cluster_by_doi(self, doi: str) -> Cluster | None:
        r = self.conn.execute("SELECT * FROM clusters WHERE doi = ? LIMIT 1", (doi,)).fetchone()
        return self._row_to_cluster(r) if r else None

    def recent_clusters(self, since: datetime) -> list[Cluster]:
        rows = self.conn.execute(
            "SELECT * FROM clusters WHERE created_at >= ? OR published_at >= ?",
            (_iso(since), _iso(since)),
        ).fetchall()
        return [self._row_to_cluster(r) for r in rows]

    def update_cluster_published(
        self, cluster_id: int, published_at: datetime | None, doi: str | None = None
    ) -> None:
        """Keep the earliest published_at; fill DOI if the cluster lacks one."""
        if published_at is None and doi is None:
            return
        with self.tx() as c:
            r = c.execute(
                "SELECT published_at, doi FROM clusters WHERE id = ?", (cluster_id,)
            ).fetchone()
            cur = _parse(r["published_at"])
            new_pub = cur
            if published_at is not None and (cur is None or published_at < cur):
                new_pub = published_at
            new_doi = r["doi"] or doi
            c.execute(
                "UPDATE clusters SET published_at = ?, doi = ? WHERE id = ?",
                (_iso(new_pub), new_doi, cluster_id),
            )

    def set_prefilter(
        self,
        cluster_id: int,
        status: str | None,
        reason: str | None = None,
        at: datetime | None = None,
    ) -> None:
        """status None = back to the queue (deferred); 'pass' / 'drop' with a reason."""
        with self.tx() as c:
            c.execute(
                """UPDATE clusters SET prefilter_status = ?, prefilter_reason = ?,
                   prefiltered_at = ? WHERE id = ?""",
                (status, reason, _iso(at or utcnow()), cluster_id),
            )

    def reset_prefilter(self, keep_reasons: tuple[str, ...] = ("stale",)) -> int:
        """Send dropped clusters back through the prefilter (after a keyword change).
        Clusters dropped for a reason in keep_reasons stay dropped. Returns the count."""
        placeholders = ",".join("?" for _ in keep_reasons) or "''"
        with self.tx() as c:
            cur = c.execute(
                f"""UPDATE clusters SET prefilter_status = NULL, prefilter_reason = NULL
                    WHERE prefilter_status = 'drop'
                      AND COALESCE(prefilter_reason, '') NOT IN ({placeholders})""",
                keep_reasons,
            )
            return int(cur.rowcount)

    def unprefiltered_clusters(self) -> list[Cluster]:
        rows = self.conn.execute(
            """SELECT * FROM clusters WHERE prefilter_status IS NULL
               ORDER BY published_at IS NULL, published_at DESC, id"""
        ).fetchall()
        return [self._row_to_cluster(r) for r in rows]

    def unscored_clusters(self, model: str, prompt_version: str) -> list[Cluster]:
        rows = self.conn.execute(
            """SELECT c.* FROM clusters c
               WHERE c.prefilter_status = 'pass'
                 AND NOT EXISTS (SELECT 1 FROM scores s WHERE s.cluster_id = c.id
                                 AND s.model = ? AND s.prompt_version = ?)
               ORDER BY c.id""",
            (model, prompt_version),
        ).fetchall()
        return [self._row_to_cluster(r) for r in rows]

    def count_prefilter_passed_since(self, since: datetime) -> int:
        """Passes recorded since `since` (by prefiltered_at; created_at for rows from before
        that column existed)."""
        r = self.conn.execute(
            """SELECT COUNT(*) FROM clusters WHERE prefilter_status = 'pass'
               AND COALESCE(prefiltered_at, created_at) >= ?""",
            (_iso(since),),
        ).fetchone()
        return int(r[0])

    # ---- scores ------------------------------------------------------------
    def insert_score(self, s: Score) -> int:
        with self.tx() as c:
            cur = c.execute(
                """INSERT INTO scores (cluster_id, model, prompt_version, novelty,
                   clinical_significance, audience_interest, expertise_fit, timeliness,
                   evidence_level, hype_risk, total, rationale, suggested_angle,
                   raw_response, scored_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    s.cluster_id,
                    s.model,
                    s.prompt_version,
                    s.novelty,
                    s.clinical_significance,
                    s.audience_interest,
                    s.expertise_fit,
                    s.timeliness,
                    s.evidence_level,
                    s.hype_risk,
                    s.total,
                    s.rationale,
                    s.suggested_angle,
                    s.raw_response,
                    _iso(s.scored_at),
                ),
            )
            return int(cur.lastrowid)

    def _row_to_score(self, r: sqlite3.Row) -> Score:
        d = dict(r)
        d["scored_at"] = _parse(d["scored_at"])
        return Score(**d)

    def latest_score(self, cluster_id: int) -> Score | None:
        r = self.conn.execute(
            "SELECT * FROM scores WHERE cluster_id = ? ORDER BY id DESC LIMIT 1", (cluster_id,)
        ).fetchone()
        return self._row_to_score(r) if r else None

    def top_scored_clusters(
        self, since: datetime, limit: int, min_total: int = 0
    ) -> list[tuple[Cluster, Score]]:
        """Top clusters (by latest score total) published or created since `since`."""
        rows = self.conn.execute(
            """SELECT c.id AS cid, s.id AS sid FROM clusters c
               JOIN scores s ON s.id = (SELECT id FROM scores WHERE cluster_id = c.id
                                        ORDER BY id DESC LIMIT 1)
               WHERE (c.published_at >= ? OR c.created_at >= ?) AND s.total >= ?
               ORDER BY s.total DESC, c.published_at DESC LIMIT ?""",
            (_iso(since), _iso(since), min_total, limit),
        ).fetchall()
        out = []
        for r in rows:
            cl = self.get_cluster(r["cid"])
            sc = self._row_to_score(
                self.conn.execute("SELECT * FROM scores WHERE id = ?", (r["sid"],)).fetchone()
            )
            out.append((cl, sc))
        return out

    # ---- ratings -----------------------------------------------------------
    def insert_rating(
        self, cluster_id: int, rating: int, note: str | None = None, rater: str = "human"
    ) -> int:
        with self.tx() as c:
            cur = c.execute(
                "INSERT INTO ratings (cluster_id, rating, note, rated_at, rater) "
                "VALUES (?,?,?,?,?)",
                (cluster_id, rating, note, _iso(utcnow()), rater),
            )
            return int(cur.lastrowid)

    def ratings_for(self, cluster_id: int) -> list[dict[str, Any]]:
        return [
            dict(r)
            for r in self.conn.execute(
                "SELECT * FROM ratings WHERE cluster_id = ? ORDER BY id", (cluster_id,)
            )
        ]

    # ---- source runs -------------------------------------------------------
    def last_run(self, source: str) -> datetime | None:
        r = self.conn.execute(
            "SELECT last_run_at FROM source_runs WHERE source = ?", (source,)
        ).fetchone()
        return _parse(r["last_run_at"]) if r else None

    def record_run(
        self,
        source: str,
        fetched: int,
        inserted: int,
        error: str | None = None,
        at: datetime | None = None,
    ) -> None:
        with self.tx() as c:
            c.execute(
                """INSERT INTO source_runs (source, last_run_at, fetched, inserted, error)
                   VALUES (?,?,?,?,?)
                   ON CONFLICT(source) DO UPDATE SET last_run_at=excluded.last_run_at,
                   fetched=excluded.fetched, inserted=excluded.inserted, error=excluded.error""",
                (source, _iso(at or utcnow()), fetched, inserted, error),
            )

    # ---- misc --------------------------------------------------------------
    def counts(self) -> dict[str, int]:
        out = {}
        for t in ("items", "clusters", "scores", "ratings"):
            out[t] = int(self.conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0])
        return out


def window_start(hours: int) -> datetime:
    return utcnow() - timedelta(hours=hours)


def dumps(obj: Any) -> str:
    return json.dumps(obj, default=str, ensure_ascii=False)
