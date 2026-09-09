"""Shared fixtures: a temp SQLite file seeded with fake step-1 items/scores rows."""

import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

from approval_queue import store

# Mirrors the step-1 schema assumed in approval_queue/store.py.
STEP1_SCHEMA = """
CREATE TABLE items (
    id TEXT PRIMARY KEY, source TEXT, url TEXT, title TEXT, abstract TEXT,
    published_at TEXT, fetched_at TEXT, dedup_hash TEXT UNIQUE, raw_json TEXT
);
CREATE TABLE scores (
    item_id TEXT REFERENCES items(id), novelty REAL, clinical_significance REAL,
    audience_interest REAL, expertise_fit REAL, timeliness REAL, total REAL,
    rationale TEXT, suggested_angle TEXT, scored_at TEXT
);
"""

URL = "https://doi.org/10.1000/xyz123"
ABSTRACT = (
    "In this phase 2 trial of 97 patients, the overall response rate was 88% and median "
    "PFS was 14.6 months."
)


def seed_item(conn, item_id, *, source="pubmed", total=8.5, hours_ago=1, url=URL):
    now = datetime.now(UTC)
    conn.execute(
        "INSERT INTO items VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            item_id,
            source,
            url,
            f"Title {item_id}",
            ABSTRACT,
            now.isoformat(),
            now.isoformat(),
            f"hash-{item_id}",
            "{}",
        ),
    )
    conn.execute(
        "INSERT INTO scores VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            item_id,
            8,
            9,
            7,
            9,
            8,
            total,
            f"rationale {item_id}",
            f"angle {item_id}",
            (now - timedelta(hours=hours_ago)).isoformat(),
        ),
    )
    conn.commit()


@pytest.fixture
def db_file(tmp_path, monkeypatch):
    path = tmp_path / "pipeline.db"
    monkeypatch.setenv("DB_PATH", str(path))
    raw = sqlite3.connect(path)
    raw.executescript(STEP1_SCHEMA)
    raw.close()
    return path


@pytest.fixture
def conn(db_file):
    c = store.connect(db_file)
    yield c
    c.close()
