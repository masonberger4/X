"""The feed page: the digest, with its editor prompt inline (yes/no plus an explanation).

Decisions are the human ground truth the rubric is tuned against, so saving one has to be
exactly what `digest.py --rate` writes: a row in `ratings` (yes = 5, no = 1) with rater
'human' and a note that starts with a reason category.
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


def test_parse_decision_accepts_yes_or_no_and_nothing_else():
    assert feed.parse_decision("yes") == "yes" and feed.parse_decision(" N ") == "no"
    for bad in ("0", "6", "", None, "maybe", "3.5"):
        with pytest.raises(ValueError):
            feed.parse_decision(bad)


def test_the_explanation_is_required():
    assert feed.parse_note("  company: no sponsor named ") == "company: no sponsor named"
    for bad in ("", "   ", None):
        with pytest.raises(ValueError):
            feed.parse_note(bad)


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
    assert entry["human_rating"]["rating"] == 4 and entry["human_rating"]["decision"] == "yes"
    assert entry["model_rating"]["rating"] == 2 and entry["model_rating"]["decision"] == "no"
    assert entry["model_rating"]["rater"] == "auto:claude-x"


def test_feed_page_lists_scored_clusters(client, cluster_id):
    body = client.get("/feed").text
    assert "Title i1" in body and "42/50" in body and "rationale i1" in body
    assert f'action="/feed/{cluster_id}/rate"' in body
    # the reason-category box sits in the form, shown when the explanation gets focus
    assert "Reason categories" in body and "<dt>catalyst</dt>" in body
    assert 'name="note"' in body and "required" in body


def test_the_threshold_can_be_ignored(client, conn):
    seed_item(conn, "weak", total=1)
    assert "Title weak" in client.get("/feed?all=1").text


def test_hide_decided_drops_stories_with_a_human_decision(client, conn, database, cluster_id):
    seed_item(conn, "fresh", total=40)
    database.insert_rating(cluster_id, 5, "catalyst: yes")
    body = client.get("/feed?hide_decided=1").text
    assert "Title fresh" in body and "Title i1" not in body
    assert "show decided" in body
    assert 'name="back" value="hours=24&amp;top=10&amp;hide_decided=1"' in body
    both = client.get("/feed").text
    assert "Title fresh" in both and "Title i1" in both and "hide decided" in both


def test_a_model_only_rating_is_not_a_decision(client, conn, database):
    cid = seed_item(conn, "auto", total=40)
    database.insert_rating(cid, 1, "beat: off", rater="auto:test")
    assert "Title auto" in client.get("/feed?hide_decided=1").text


def test_the_feed_shows_at_most_100_stories(client, conn):
    for i in range(101):
        seed_item(conn, f"n{i}", total=40)
    assert feed.clamp_top_n(500) == 100 and feed.clamp_top_n(0) == 1
    body = client.get("/feed?top=500").text
    assert "Top 100 scored" in body and body.count('class="entry"') == 100


def test_saving_a_decision_writes_a_human_row_and_returns_to_the_window(
    client, database, cluster_id
):
    r = client.post(
        f"/feed/{cluster_id}/rate",
        data={"decision": "yes", "note": "catalyst: PDUFA in Q4", "back": "hours=72"},
    )
    assert r.status_code == 303 and r.headers["location"].startswith("/feed?hours=72")
    (row,) = database.ratings_for(cluster_id)
    assert row["rating"] == 5 and row["note"] == "catalyst: PDUFA in Q4" and row["rater"] == "human"
    r = client.post(f"/feed/{cluster_id}/rate", data={"decision": "no", "note": "beat: off"})
    assert r.status_code == 303 and database.ratings_for(cluster_id)[-1]["rating"] == 1


def test_a_bad_decision_or_a_missing_explanation_is_refused_and_an_unknown_cluster_is_404(
    client, database, cluster_id
):
    bad = {"decision": "maybe", "note": "x"}
    assert client.post(f"/feed/{cluster_id}/rate", data=bad).status_code == 400
    no_note = {"decision": "yes", "note": " "}
    assert client.post(f"/feed/{cluster_id}/rate", data=no_note).status_code == 400
    assert client.post("/feed/9999/rate", data={"decision": "no", "note": "x"}).status_code == 404
    assert database.ratings_for(cluster_id) == []
