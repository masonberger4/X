"""Meeting-window cadence in ingest/base.py (step 6). Explicit `now` everywhere."""

from datetime import UTC, datetime, timedelta

from ingest.base import Item, Source, parse_windows


class Dummy(Source):
    type = "dummy"

    def fetch(self) -> list[Item]:
        return []


WINDOWS = [{"start": "2026-11-04", "end": "2026-12-18", "cadence_minutes": 60}]


def _src(**extra):
    return Dummy({"name": "d", "cadence_minutes": 1440, **extra}, {})


def test_no_windows_is_unchanged_behaviour():
    src = _src()
    now = datetime(2026, 11, 20, 12, 0, tzinfo=UTC)
    assert src.is_due(None, now)
    assert not src.is_due(now - timedelta(hours=23), now)
    assert src.is_due(now - timedelta(hours=24), now)
    assert src.effective_cadence_minutes(now) == 1440


def test_inside_window_uses_window_cadence():
    src = _src(windows=WINDOWS)
    now = datetime(2026, 11, 20, 12, 0, tzinfo=UTC)
    assert src.effective_cadence_minutes(now) == 60
    assert not src.is_due(now - timedelta(minutes=59), now)
    assert src.is_due(now - timedelta(minutes=60), now)


def test_start_and_end_days_are_inclusive():
    src = _src(windows=WINDOWS)
    for now in (
        datetime(2026, 11, 4, 0, 0, tzinfo=UTC),
        datetime(2026, 12, 18, 23, 59, tzinfo=UTC),
    ):
        assert src.effective_cadence_minutes(now) == 60
        assert src.is_due(now - timedelta(minutes=61), now)


def test_day_after_end_falls_back_to_default():
    src = _src(windows=WINDOWS)
    now = datetime(2026, 12, 19, 0, 0, tzinfo=UTC)
    assert src.effective_cadence_minutes(now) == 1440
    assert not src.is_due(now - timedelta(hours=2), now)
    before = datetime(2026, 11, 3, 23, 59, tzinfo=UTC)
    assert src.effective_cadence_minutes(before) == 1440


def test_disabled_source_is_never_due():
    src = _src(windows=WINDOWS, enabled=False)
    now = datetime(2026, 11, 20, 12, 0, tzinfo=UTC)
    assert not src.is_due(None, now)
    assert not src.is_due(now - timedelta(days=30), now)


def test_window_without_cadence_keeps_default_and_malformed_entries_are_skipped():
    src = _src(windows=[{"start": "2026-11-04", "end": "2026-12-18"}, "junk", {"start": "x"}])
    now = datetime(2026, 11, 20, 12, 0, tzinfo=UTC)
    assert src.active_window(now) is not None
    assert src.effective_cadence_minutes(now) == 1440
    assert len(src.windows) == 1


def test_parse_windows_accepts_dates_and_rejects_reversed():
    ws = parse_windows(
        [
            {
                "start": datetime(2027, 4, 1, tzinfo=UTC).date(),
                "end": "2027-04-07",
                "cadence_minutes": "30",
            },
            {"start": "2027-04-10", "end": "2027-04-01"},
        ]
    )
    assert len(ws) == 1 and ws[0][2] == 30
