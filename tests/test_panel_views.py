"""The panel's view models are pure: no DB, no clock, no network."""

from datetime import UTC, datetime, timedelta

import pytest

from ops.models import Check, Report, SourceRun
from ops.runner import Step
from panel import views

NOW = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)


def test_fmt_age_and_duration():
    assert views.fmt_age(NOW, None) == "never"
    assert views.fmt_age(NOW, NOW - timedelta(minutes=40)) == "40m ago"
    assert views.fmt_age(NOW, NOW - timedelta(hours=3)) == "3.0h ago"
    assert views.fmt_age(NOW, NOW - timedelta(days=3)) == "3.0d ago"
    assert views.fmt_duration(None) == ""
    assert views.fmt_duration(42) == "42s"
    assert views.fmt_duration(150) == "2.5m"


def test_check_rows_put_the_worst_first():
    report = Report.build(
        NOW,
        [
            Check("env", "ok", "fine"),
            Check("sources", "warn", "2 stale"),
            Check("backups", "fail", "no backup"),
            Check("feedback", "skip", "not merged"),
        ],
    )
    assert [r["name"] for r in views.check_rows(report)] == [
        "backups",
        "sources",
        "env",
        "feedback",
    ]
    assert views.check_rows(None) == []


@pytest.mark.parametrize(
    "run, expected",
    [
        (None, ("skip", "never run")),
        ({"skipped_reason": "disabled"}, ("skip", "skipped (disabled)")),
        ({"skipped_reason": None, "timed_out": True}, ("fail", "timed out")),
        ({"skipped_reason": None, "timed_out": False, "exit_code": 0}, ("ok", "exit 0")),
        ({"skipped_reason": None, "timed_out": False, "exit_code": 2}, ("fail", "exit 2")),
    ],
)
def test_outcome_of(run, expected):
    assert views.outcome_of(run) == expected


def test_step_rows_carry_the_configured_command_and_last_outcome():
    steps = [
        Step("ingest", ["python", "run_ingest.py"], required=True),
        Step("publish", ["python", "run_publish.py"], enabled=False),
    ]
    last = {
        "ingest": {
            "skipped_reason": None,
            "timed_out": False,
            "exit_code": 0,
            "finished_at": NOW - timedelta(minutes=20),
        }
    }
    rows = views.step_rows(steps, last, NOW)
    assert rows[0]["status"] == "ok" and rows[0]["age"] == "20m ago"
    assert rows[0]["argv"] == "python run_ingest.py" and rows[0]["required"]
    assert rows[1]["enabled"] is False and rows[1]["outcome"] == "never run"


def test_source_rows_flag_errors_then_never_ran_then_stale():
    runs = [
        SourceRun("fresh", cadence_minutes=60, last_run_at=NOW - timedelta(minutes=30)),
        SourceRun("stale", cadence_minutes=60, last_run_at=NOW - timedelta(hours=10)),
        SourceRun("never", cadence_minutes=60),
        SourceRun("broken", cadence_minutes=60, last_run_at=NOW, error="HTTP 500"),
        SourceRun("off", enabled=False),
    ]
    rows = {r["source"]: r for r in views.source_rows(runs, NOW, stale_multiplier=3)}
    assert rows["fresh"]["status"] == "ok"
    assert rows["stale"]["status"] == "warn"
    assert rows["never"]["status"] == "warn" and rows["never"]["age"] == "never"
    assert rows["broken"]["status"] == "fail" and rows["broken"]["error"] == "HTTP 500"
    assert rows["off"]["status"] == "skip"
    # worst first
    assert views.source_rows(runs, NOW, 3)[0]["source"] == "broken"
