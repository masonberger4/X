"""Pure scheduler tests: slot math across DST, selection policy, breaking detection."""

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from publish.scheduler import (
    Policy,
    is_breaking,
    load_publish_config,
    next_slot,
    open_slot,
    pick_for_slot,
    slot_label,
)
from publish.store import Approved

NY = "America/New_York"
SLOTS = ["08:30", "12:15"]


def approved(draft_id=1, source="pubmed", title="A paper", score=30.0, approved_at="2026-01-01"):
    return Approved(
        draft_id=draft_id,
        item_id=f"i{draft_id}",
        cluster_id=draft_id,
        source=source,
        url="https://doi.org/10.1/x",
        title=title,
        single_post="p https://doi.org/10.1/x",
        thread=[],
        score=score,
        approved_at=approved_at,
    )


def test_config_loads_with_defaults():
    cfg = load_publish_config()
    assert cfg["timezone"] and cfg["slots"] and cfg["max_posts_per_day"] >= 1
    assert cfg["breaking"]["source_prefixes"]


def test_next_slot_same_day_and_rollover():
    now = datetime(2026, 6, 1, 9, 0, tzinfo=ZoneInfo(NY))
    nxt = next_slot(now, SLOTS, NY)
    assert (nxt.hour, nxt.minute, nxt.day) == (12, 15, 1)
    late = datetime(2026, 6, 1, 13, 0, tzinfo=ZoneInfo(NY))
    nxt = next_slot(late, SLOTS, NY)
    assert (nxt.hour, nxt.minute, nxt.day) == (8, 30, 2)


def test_next_slot_across_spring_forward():
    # 2026-03-08 02:00 EST -> 03:00 EDT. 01:30 EST is 06:30 UTC; 08:30 EDT is 12:30 UTC.
    now = datetime(2026, 3, 8, 6, 30, tzinfo=UTC)
    nxt = next_slot(now, SLOTS, NY)
    assert nxt.strftime("%H:%M %Z") == "08:30 EDT"
    assert nxt - now == timedelta(hours=6)  # only 6 real hours, not 7


def test_next_slot_across_fall_back():
    # 2026-11-01 02:00 EDT -> 01:00 EST. 23:00 EDT on Oct 31 is 03:00 UTC Nov 1.
    now = datetime(2026, 11, 1, 3, 0, tzinfo=UTC)
    nxt = next_slot(now, SLOTS, NY)
    assert nxt.strftime("%Y-%m-%d %H:%M %Z") == "2026-11-01 08:30 EST"
    assert nxt - now == timedelta(hours=10, minutes=30)  # 9.5h on the wall clock + 1h


def test_next_slot_requires_aware_datetime():
    with pytest.raises(ValueError):
        next_slot(datetime(2026, 1, 1, 9), SLOTS, NY)


def test_open_slot_window():
    zone = ZoneInfo(NY)
    assert open_slot(datetime(2026, 6, 1, 8, 29, tzinfo=zone), SLOTS, NY, 20) is None
    s = open_slot(datetime(2026, 6, 1, 8, 44, tzinfo=zone), SLOTS, NY, 20)
    assert s is not None and slot_label(s) == "2026-06-01 08:30"
    assert open_slot(datetime(2026, 6, 1, 8, 50, tzinfo=zone), SLOTS, NY, 20) is None


def test_is_breaking_rules():
    cfg = load_publish_config()["breaking"]
    assert is_breaking(approved(source="fda_oce_approvals"), cfg)
    assert is_breaking(approved(source="fda_press", title="anything"), cfg)
    assert is_breaking(approved(source="company_kite", title="FDA Approves X"), cfg)
    assert not is_breaking(approved(source="company_kite", title="Q2 earnings"), cfg)
    assert not is_breaking(approved(source="pubmed", title="approval of X"), cfg)


def test_pick_prefers_breaking_then_score_then_oldest():
    slot = datetime(2026, 6, 1, 8, 30, tzinfo=ZoneInfo(NY))
    pol = Policy()
    a = approved(1, score=40, approved_at="2026-05-30")
    b = approved(2, score=45, approved_at="2026-05-31")
    c = approved(3, source="fda_press", score=10)
    assert pick_for_slot([a, b, c], slot, pol).draft_id == 3
    assert pick_for_slot([a, b], slot, pol).draft_id == 2
    a2 = approved(4, score=45, approved_at="2026-05-29")
    assert pick_for_slot([a, b, a2], slot, pol).draft_id == 4
    assert pick_for_slot([], slot, pol) is None


def test_pick_respects_daily_cap_and_min_gap():
    slot = datetime(2026, 6, 1, 12, 15, tzinfo=ZoneInfo(NY))
    assert pick_for_slot([approved()], slot, Policy(max_posts_per_day=2, posted_today=2)) is None
    recent = Policy(min_gap_minutes=90, last_posted_at=slot - timedelta(minutes=30))
    assert pick_for_slot([approved()], slot, recent) is None
    assert "min gap" in recent.blocked_reason(slot)
    ok = Policy(min_gap_minutes=90, last_posted_at=slot - timedelta(minutes=95))
    assert pick_for_slot([approved()], slot, ok) is not None


def test_rank_puts_the_human_order_before_breaking_and_score():
    from publish.scheduler import Policy, rank
    from publish.store import Approved

    def a(i, **kw):
        base = dict(draft_id=i, item_id=f"i{i}", cluster_id=i, source="pubmed", url="", title="")
        base.update(kw)
        return Approved(single_post="x", **base)

    fda = a(1, source="fda_oce", score=10.0)
    high = a(2, score=50.0)
    second = a(3, score=1.0, position=2)
    first = a(4, score=2.0, position=1)
    got = [x.draft_id for x in rank([fda, high, second, first], Policy())]
    assert got == [4, 3, 1, 2]


def test_save_caps_edits_only_those_lines_and_keeps_comments(tmp_path):
    from publish.scheduler import load_publish_config, save_caps

    cfg = tmp_path / "publish.yaml"
    cfg.write_text(
        "timezone: UTC\n# limits\nmax_posts_per_day: 3   # per day\nmin_gap_minutes: 90\n"
        "slots:\n  - '08:30'\n"
    )
    assert save_caps(5, 30, cfg) == {"max_posts_per_day": 5, "min_gap_minutes": 30}
    text = cfg.read_text()
    assert "max_posts_per_day: 5  # per day\n" in text and "min_gap_minutes: 30\n" in text
    assert "# limits" in text and "timezone: UTC" in text
    loaded = load_publish_config(cfg)
    assert loaded["max_posts_per_day"] == 5 and loaded["min_gap_minutes"] == 30
    with pytest.raises(ValueError):
        save_caps(0, 30, cfg)
    with pytest.raises(ValueError):
        save_caps(1, -1, cfg)
    # a file without the keys gets them appended
    cfg.write_text("timezone: UTC")
    save_caps(2, 10, cfg)
    assert load_publish_config(cfg)["min_gap_minutes"] == 10
