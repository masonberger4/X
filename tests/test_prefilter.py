from datetime import UTC, datetime, timedelta

from filter.dedup import assign_cluster
from filter.prefilter import check, run_prefilter
from ingest.base import Item

CFG = {
    "require_abstract": True,
    "min_abstract_chars": 20,
    "daily_cap": 2,
    "allow_keywords": ["cancer", "CAR-T"],
    "deny_keywords": ["webinar"],
}


def test_check_rules():
    assert check("CAR-T durable responses", "x" * 30, CFG) == (True, None)
    assert check("Join our webinar on cancer", "x" * 30, CFG) == (False, "deny:webinar")
    assert check("Quarterly earnings", "x" * 30, CFG) == (False, "no_allow_keyword")
    assert check("Cancer study", "short", CFG) == (False, "abstract<20")
    assert check("Cancer study", "", {"require_abstract": False, "allow_keywords": ["cancer"]}) == (
        True,
        None,
    )


def test_run_prefilter_marks_clusters_and_applies_cap(db):
    titles = [
        "Pembrolizumab in early breast cancer",
        "CAR-T for pediatric leukemia",
        "Colorectal cancer screening uptake",
        "Earnings call",
    ]
    now = datetime.now(UTC)
    for i, t in enumerate(titles):  # cluster 1 is the oldest, 3 the newest
        assign_cluster(
            db,
            Item.build(
                source="s",
                url=f"https://x/{i}",
                title=t,
                abstract="a" * 50,
                published_at=now - timedelta(days=5 - i),
            ),
        )
    counts = run_prefilter(db, CFG, now=now)
    assert counts == {"pass": 2, "daily_cap": 1, "no_allow_keyword": 1}
    statuses = [
        (c.prefilter_status, c.prefilter_reason) for c in (db.get_cluster(i) for i in range(1, 5))
    ]
    # newest-first: the oldest cancer cluster is the one the cap drops
    assert statuses == [
        ("drop", "daily_cap"),
        ("pass", None),
        ("pass", None),
        ("drop", "no_allow_keyword"),
    ]
    assert run_prefilter(db, CFG) == {"pass": 0}  # nothing left unfiltered
