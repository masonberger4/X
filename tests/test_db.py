from datetime import datetime, timedelta, timezone

from db import Score
from ingest.base import Item, compute_dedup_hash, normalize_title, normalize_url, normalize_doi


def _item(source="s", url="https://x.org/a", title="Hello World", **kw):
    return Item.build(source=source, url=url, title=title, **kw)


def test_normalizers():
    assert normalize_title("  Héllo,  World! ") == "hello world"
    assert normalize_url("HTTPS://Example.com/Path/?utm_source=x&b=2#frag") == "https://example.com/Path?b=2"
    assert normalize_doi("https://doi.org/10.1056/NEJMoa2400001.") == "10.1056/nejmoa2400001"
    assert normalize_doi("no doi here") is None
    assert compute_dedup_hash("A b", "http://X.com/") == compute_dedup_hash("a  B", "http://x.com")


def test_item_build_extracts_doi_from_url():
    it = _item(url="https://doi.org/10.1038/s41591-024-0001-1")
    assert it.doi == "10.1038/s41591-024-0001-1"
    assert len(it.dedup_hash) == 64


def test_insert_and_dedup_unique(db):
    it = _item()
    assert db.insert_item(it) is True
    assert db.insert_item(_item()) is False
    assert db.item_exists(it.dedup_hash)
    got = db.get_item(it.id)
    assert got.title == "Hello World"
    assert db.counts()["items"] == 1


def test_cluster_roundtrip_and_earliest_published(db):
    t1 = datetime(2026, 1, 2, tzinfo=timezone.utc)
    t0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
    cid = db.create_cluster("T", normalize_title("T"), None, t1)
    db.update_cluster_published(cid, t0, "10.1056/abc")
    cl = db.get_cluster(cid)
    assert cl.published_at == t0 and cl.doi == "10.1056/abc"
    db.update_cluster_published(cid, t1 + timedelta(days=1))
    assert db.get_cluster(cid).published_at == t0
    it = _item()
    it.cluster_id = cid
    db.insert_item(it)
    assert db.get_cluster(cid).member_ids == [it.id]
    assert db.find_cluster_by_doi("10.1056/abc").id == cid


def test_scores_ratings_and_top(db):
    now = datetime.now(timezone.utc)
    cids = [db.create_cluster(f"T{i}", f"t{i}", None, now) for i in range(3)]
    for cid in cids:
        db.set_prefilter(cid, "pass")
    assert len(db.unscored_clusters("m", "v1")) == 3
    for cid, total in zip(cids, (10, 50, 30)):
        db.insert_score(Score(
            cluster_id=cid, model="m", prompt_version="v1", novelty=1, clinical_significance=1,
            audience_interest=1, expertise_fit=1, timeliness=1, evidence_level="phase3",
            hype_risk=1, total=total, rationale="r", suggested_angle="a", raw_response="{}",
            scored_at=now))
    assert db.unscored_clusters("m", "v1") == []
    assert len(db.unscored_clusters("m", "v2")) == 3
    top = db.top_scored_clusters(now - timedelta(hours=1), limit=2, min_total=20)
    assert [s.total for _, s in top] == [50, 30]
    db.insert_rating(cids[1], 4, "good")
    assert db.ratings_for(cids[1])[0]["rating"] == 4
    assert db.latest_score(cids[0]).total == 10


def test_source_runs(db):
    assert db.last_run("x") is None
    db.record_run("x", 5, 2)
    assert db.last_run("x") is not None
    db.record_run("x", 0, 0, error="boom")
    assert db.conn.execute("SELECT error FROM source_runs").fetchone()[0] == "boom"
