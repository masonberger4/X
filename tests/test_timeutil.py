"""The display timezone: storage stays UTC, only what a human reads is converted."""

from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

import timeutil


def test_the_configured_zone_is_seattle():
    """The shipped root config.yaml drives every human-facing timestamp."""
    assert timeutil.timezone_name() == "America/Los_Angeles"
    assert timeutil.display_tz() == ZoneInfo("America/Los_Angeles")


def test_config_and_behaviour_zones_agree():
    """publish/ and feedback/ keep their own keys; they must name the same zone."""
    import yaml

    from publish.scheduler import load_publish_config

    assert load_publish_config()["timezone"] == timeutil.timezone_name()
    feedback_cfg = yaml.safe_load(Path("feedback/config.yaml").read_text(encoding="utf-8"))
    assert feedback_cfg["timezone"] == timeutil.timezone_name()


@pytest.mark.parametrize(
    ("utc", "expected"),
    [
        # Summer: Pacific is UTC-7.
        (datetime(2026, 6, 1, 15, 30, tzinfo=UTC), "2026-06-01 08:30 PDT"),
        # Winter: UTC-8, and the abbreviation follows.
        (datetime(2026, 1, 15, 15, 30, tzinfo=UTC), "2026-01-15 07:30 PST"),
        # Before 08:00 UTC the Pacific calendar day is still the previous one.
        (datetime(2026, 6, 2, 3, 0, tzinfo=UTC), "2026-06-01 20:00 PDT"),
    ],
)
def test_fmt_datetime_converts_and_labels_the_zone(utc, expected):
    assert timeutil.fmt_datetime(utc) == expected


def test_a_naive_value_is_read_as_utc():
    """Every store in this repo writes UTC, so a naive value is never local time."""
    naive = datetime(2026, 6, 1, 15, 30)
    assert timeutil.fmt_datetime(naive) == timeutil.fmt_datetime(naive.replace(tzinfo=UTC))


def test_stored_iso_strings_are_accepted():
    """Templates render values straight out of SQLite without parsing them first."""
    assert timeutil.fmt_datetime("2026-06-01T15:30:00+00:00") == "2026-06-01 08:30 PDT"
    assert timeutil.fmt_datetime("2026-06-01T15:30:00Z") == "2026-06-01 08:30 PDT"
    assert timeutil.fmt_date("2026-06-02T03:00:00+00:00") == "2026-06-01"


def test_a_missing_or_unparseable_timestamp_renders_as_nothing():
    for value in (None, "", "   ", "not a date"):
        assert timeutil.fmt_datetime(value) == ""
        assert timeutil.fmt_date(value) == ""
        assert timeutil.to_display(value) is None


def test_an_unknown_zone_in_config_falls_back_instead_of_raising(tmp_path, monkeypatch):
    cfg = tmp_path / "config.yaml"
    cfg.write_text("timezone: Mars/Olympus_Mons\n", encoding="utf-8")
    monkeypatch.setattr(timeutil, "_CONFIG_PATH", cfg)
    timeutil.timezone_name.cache_clear()
    try:
        assert timeutil.timezone_name() == timeutil.DEFAULT_TIMEZONE
    finally:
        timeutil.timezone_name.cache_clear()


def test_a_missing_config_falls_back(tmp_path, monkeypatch):
    monkeypatch.setattr(timeutil, "_CONFIG_PATH", tmp_path / "nope.yaml")
    timeutil.timezone_name.cache_clear()
    try:
        assert timeutil.timezone_name() == timeutil.DEFAULT_TIMEZONE
    finally:
        timeutil.timezone_name.cache_clear()


def test_jinja_filters_are_installed():
    from jinja2 import Environment

    env = Environment(autoescape=True)
    timeutil.install_jinja_filters(env)
    out = env.from_string("{{ t|localtime }} / {{ t|localdate }}").render(
        t="2026-06-01T15:30:00+00:00"
    )
    assert out == "2026-06-01 08:30 PDT / 2026-06-01"
