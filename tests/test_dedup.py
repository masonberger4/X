from datetime import datetime, timezone

from filter.dedup import assign_cluster, title_similarity
from ingest.base import Item


def _item(source, url, title, doi=None, published_at=None, abstract=""):
    return Item.build(source=source, url=url, title=title, doi=doi,
                      published_at=published_at, abstract=abstract)


def test_exact_hash_skips(db, dedup_cfg):
    a = _item("rss", "https://a.org/1", "CAR-T shows durable responses")
    ins, cid = assign_cluster(db, a, dedup_cfg)
    assert ins and cid == 1
    ins2, cid2 = assign_cluster(db, _item("rss", "https://a.org/1?utm_source=x", "CAR-T shows durable responses"), dedup_cfg)
    assert not ins2 and cid2 is None
    assert db.counts() == {"items": 1, "clusters": 1, "scores": 0, "ratings": 0}


def test_doi_match_joins_cluster(db, dedup_cfg):
    t0 = datetime(2026, 3, 1, tzinfo=timezone.utc)
    t1 = datetime(2026, 3, 2, tzinfo=timezone.utc)
    a = _item("pubmed", "https://pubmed.gov/1", "Trial of drug X in NSCLC", doi="10.1056/abc", published_at=t1)
    b = _item("nejm", "https://nejm.org/doi/10.1056/ABC", "Completely different title about X", published_at=t0)
    _, c1 = assign_cluster(db, a, dedup_cfg)
    ins, c2 = assign_cluster(db, b, dedup_cfg)
    assert ins and c1 == c2
    cl = db.get_cluster(c1)
    assert cl.published_at == t0
    assert set(cl.member_ids) == {a.id, b.id}


def test_near_duplicate_title_clusters(db, dedup_cfg):
    a = _item("eurekalert", "https://e.org/1", "New CAR-T therapy shows durable responses in myeloma")
    b = _item("company_x", "https://x.com/pr", "New CAR-T therapy shows durable responses in myeloma.")
    c = _item("nature", "https://n.org/9", "Gut microbiome shapes checkpoint inhibitor response")
    _, c1 = assign_cluster(db, a, dedup_cfg)
    _, c2 = assign_cluster(db, b, dedup_cfg)
    _, c3 = assign_cluster(db, c, dedup_cfg)
    assert c1 == c2 != c3
    assert db.counts()["clusters"] == 2


def test_similarity_threshold_respected(db):
    a = _item("s", "https://a/1", "Pembrolizumab improves survival in melanoma")
    b = _item("s", "https://a/2", "Pembrolizumab improves survival in lung cancer")
    assert title_similarity(a.title, b.title) < 0.92
    _, c1 = assign_cluster(db, a, {"title_similarity": 0.92})
    _, c2 = assign_cluster(db, b, {"title_similarity": 0.92})
    assert c1 != c2
