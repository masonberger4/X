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
    assert counts == {"pass": 2, "deferred": 1, "no_allow_keyword": 1}
    statuses = [
        (c.prefilter_status, c.prefilter_reason) for c in (db.get_cluster(i) for i in range(1, 5))
    ]
    # newest-first: the oldest cancer cluster is the one the cap defers (status stays NULL)
    assert statuses == [
        (None, None),
        ("pass", None),
        ("pass", None),
        ("drop", "no_allow_keyword"),
    ]
    # same day, cap still full: still deferred
    assert run_prefilter(db, CFG, now=now) == {"pass": 0, "deferred": 1}
    # next day: the deferred cluster passes
    assert run_prefilter(db, CFG, now=now + timedelta(days=1)) == {"pass": 1}
    assert db.get_cluster(1).prefilter_status == "pass"


def test_deferred_cluster_older_than_max_age_is_dropped_as_stale(db):
    now = datetime.now(UTC)
    for i, age in enumerate((1, 30)):  # newest first: the 1-day-old one takes the cap slot
        assign_cluster(
            db,
            Item.build(
                source="s",
                url=f"https://x/{i}",
                title=f"Cancer story {i}",
                abstract="a" * 50,
                published_at=now - timedelta(days=age),
            ),
        )
    cfg = {**CFG, "daily_cap": 1, "max_age_days": 7}
    assert run_prefilter(db, cfg, now=now) == {"pass": 1, "stale": 1}
    assert db.get_cluster(2).prefilter_reason == "stale"


def test_reset_prefilter_requeues_drops_except_stale(db):
    for i, t in enumerate(("T-cell engager readout", "Earnings call", "Old cancer news")):
        assign_cluster(db, Item.build(source="s", url=f"https://x/{i}", title=t, abstract="a" * 50))
    run_prefilter(db, CFG)
    db.set_prefilter(3, "drop", "stale")
    # keyword list widened: the engager story should now qualify
    wider = {**CFG, "allow_keywords": CFG["allow_keywords"] + ["engager"]}
    assert run_prefilter(db, wider) == {"pass": 0}  # nothing re-evaluated without a reset
    assert db.reset_prefilter() == 2  # engager + earnings; stale stays
    counts = run_prefilter(db, wider)
    assert counts["pass"] == 1 and counts["no_allow_keyword"] == 1
    assert db.get_cluster(3).prefilter_reason == "stale"
