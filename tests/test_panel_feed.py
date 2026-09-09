"""The feed page: the digest, with its rating prompt inline.

Ratings are the human ground truth the rubric is tuned against, so saving one has to be
exactly what `digest.py --rate` writes: a row in `ratings` with rater 'human'.
"""

import pytest
from fastapi.testclient import TestClient

from db import Database
from panel import feed
from panel.app import app
from tests.conftest import seed_item


@pytest.fixture
def client(db_file):
    return TestClient(app, follow_redirects=False)


@pytest.fixture
def database(db_file):
    d = Database(str(db_file))
    yield d
    d.close()


@pytest.fixture
def cluster_id(conn):
    return seed_item(conn, "i1", source="biorxiv", total=42)


def test_parse_rating_accepts_1_to_5_and_nothing_else():
    assert feed.parse_rating("3") == 3
    for bad in ("0", "6", "", None, "four", "3.5"):
        with pytest.raises(ValueError):
            feed.parse_rating(bad)


def test_entry_views_carry_the_score_breakdown_and_the_source(database, cluster_id):
    rows = feed.fetch_entries(database, hours=24, top_n=10, min_total=0)
    (entry,) = feed.entry_views(database, rows)
    assert entry["cluster_id"] == cluster_id and entry["total"] == 42
    assert entry["source"] == "biorxiv" and entry["rationale"] == "rationale i1"
    assert dict(entry["parts"])["clinical"] == 9
    assert entry["human_rating"] is None and entry["model_rating"] is None


def test_entry_views_clip_a_long_abstract(database, conn):
    seed_item(conn, "long", total=9, abstract="x" * 2000)
    rows = feed.fetch_entries(database, hours=24, top_n=10, min_total=0)
    entry = next(e for e in feed.entry_views(database, rows) if e["title"] == "Title long")
    assert len(entry["abstract"]) == feed.MAX_ABSTRACT_CHARS + 1
    assert entry["abstract"].endswith("…")


def test_entry_views_separate_the_human_rating_from_the_model_one(database, cluster_id):
    database.insert_rating(cluster_id, 4, "good angle")
    database.insert_rating(cluster_id, 2, "meh", rater="auto:claude-x")
    rows = feed.fetch_entries(database, hours=24, top_n=10, min_total=0)
    (entry,) = feed.entry_views(database, rows)
    assert entry["human_rating"]["rating"] == 4
    assert entry["model_rating"]["rating"] == 2
    assert entry["model_rating"]["rater"] == "auto:claude-x"


def test_feed_page_lists_scored_clusters(client, cluster_id):
    body = client.get("/feed").text
    assert "Title i1" in body and "42/50" in body and "rationale i1" in body
    assert f'action="/feed/{cluster_id}/rate"' in body


def test_the_threshold_can_be_ignored(client, conn):
    seed_item(conn, "weak", total=1)
    assert "Title weak" in client.get("/feed?all=1").text


def test_saving_a_rating_writes_a_human_row_and_returns_to_the_window(client, database, cluster_id):
    r = client.post(
        f"/feed/{cluster_id}/rate", data={"rating": "5", "note": "post this", "back": "hours=72"}
    )
    assert r.status_code == 303 and r.headers["location"].startswith("/feed?hours=72")
    (row,) = database.ratings_for(cluster_id)
    assert row["rating"] == 5 and row["note"] == "post this" and row["rater"] == "human"


def test_a_bad_rating_is_refused_and_an_unknown_cluster_is_404(client, database, cluster_id):
    assert client.post(f"/feed/{cluster_id}/rate", data={"rating": "9"}).status_code == 400
    assert client.post("/feed/9999/rate", data={"rating": "3"}).status_code == 404
    assert database.ratings_for(cluster_id) == []
