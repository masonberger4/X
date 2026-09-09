"""ops/health.py: pure checks with hand-built dataclasses and a fixed clock."""

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from ops import health
from ops.health import Thresholds
from ops.models import (
    Check,
    FeedbackState,
    PublishState,
    Report,
    SourceRun,
    StageActivity,
    overall_status,
)

NOW = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
TH = Thresholds()


def ago(hours: float) -> datetime:
    return NOW - timedelta(hours=hours)


# ---- sources ----------------------------------------------------------------


def test_sources_ok():
    runs = [SourceRun("a", cadence_minutes=60, last_run_at=ago(0.5))]
    c = health.check_sources(runs, NOW, TH)
    assert c.status == "ok" and c.details["failing"] == 0


def test_sources_error_vs_never_ran_vs_stale():
    runs = [
        SourceRun("err", cadence_minutes=60, last_run_at=ago(0.5), error="HTTP 503"),
        SourceRun("never", cadence_minutes=60),
        SourceRun("stale", cadence_minutes=60, last_run_at=ago(4)),  # > 3 x 60 min
        SourceRun("ok1", cadence_minutes=60, last_run_at=ago(1)),
        SourceRun("ok2", cadence_minutes=60, last_run_at=ago(1)),
        SourceRun("ok3", cadence_minutes=60, last_run_at=ago(1)),
        SourceRun("ok4", cadence_minutes=60, last_run_at=ago(1)),
        SourceRun("ok5", cadence_minutes=60, last_run_at=ago(1)),
        SourceRun("ok6", cadence_minutes=60, last_run_at=ago(1)),
        SourceRun("ok7", cadence_minutes=60, last_run_at=ago(1)),
        SourceRun("off", enabled=False),  # disabled: ignored entirely
    ]
    c = health.check_sources(runs, NOW, TH)
    assert c.status == "warn"  # 3/10 < 0.34
    assert c.details["error"] == ["err"]
    assert c.details["never_ran"] == ["never"]
    assert c.details["stale"] == ["stale"]
    assert c.details["enabled"] == 10
    assert "HTTP 503" not in c.summary  # error text is not echoed into summaries


def test_sources_failure_fraction_fails():
    runs = [
        SourceRun("a", last_run_at=ago(1), error="x"),
        SourceRun("b"),
        SourceRun("c", last_run_at=ago(1)),
    ]
    c = health.check_sources(runs, NOW, TH)
    assert c.status == "fail"  # 2/3 >= 0.34


def test_sources_none_enabled_is_skip():
    assert health.check_sources([], NOW, TH).status == "skip"
    assert health.check_sources([SourceRun("a", enabled=False)], NOW, TH).status == "skip"


# ---- staleness ----------------------------------------------------------------


def activity(**kw) -> StageActivity:
    base = dict(
        tables_present=True,
        latest_fetched_at=ago(0.5),
        latest_scored_at=ago(1),
        latest_draft_at=ago(2),
        unscored_backlog=5,
    )
    base.update(kw)
    return StageActivity(**base)


@pytest.mark.parametrize(
    "field,limit",
    [
        ("latest_fetched_at", TH.max_hours_since_ingest),
        ("latest_scored_at", TH.max_hours_since_score),
        ("latest_draft_at", TH.max_hours_since_draft),
    ],
)
def test_staleness_just_inside_and_outside_each_threshold(field, limit):
    inside = health.check_staleness(activity(**{field: ago(limit - 0.01)}), NOW, TH)
    assert inside.status == "ok", inside.summary
    outside = health.check_staleness(activity(**{field: ago(limit + 0.01)}), NOW, TH)
    assert outside.status == "warn", outside.summary
    way_out = health.check_staleness(activity(**{field: ago(2 * limit + 0.01)}), NOW, TH)
    assert way_out.status == "fail", way_out.summary


def test_staleness_never_is_fail():
    c = health.check_staleness(activity(latest_fetched_at=None), NOW, TH)
    assert c.status == "fail"
    assert "never" in c.summary


def test_staleness_score_lag_with_empty_backlog_is_ok():
    # Nothing to score: an old scored_at is not a problem.
    c = health.check_staleness(activity(latest_scored_at=ago(100), unscored_backlog=0), NOW, TH)
    assert c.details["statuses"]["score"] == "ok"
    assert c.status == "ok"


def test_staleness_skip_without_tables():
    c = health.check_staleness(StageActivity(tables_present=False), NOW, TH)
    assert c.status == "skip"


# ---- backlog / budget ---------------------------------------------------------


def test_backlog_caps():
    assert health.check_backlog(activity(unscored_backlog=100), NOW, TH).status == "ok"
    assert health.check_backlog(activity(unscored_backlog=101), NOW, TH).status == "warn"
    assert health.check_backlog(activity(unscored_backlog=201), NOW, TH).status == "fail"
    old = activity(pending_drafts=3, oldest_pending_age_hours=73)
    c = health.check_backlog(old, NOW, TH)
    assert c.status == "warn" and "oldest pending" in c.summary
    fresh = activity(pending_drafts=3, oldest_pending_age_hours=71)
    assert health.check_backlog(fresh, NOW, TH).status == "ok"


def test_budget_caps():
    assert health.check_budget(activity(scores_24h=100, drafts_24h=5), TH).status == "ok"
    assert health.check_budget(activity(scores_24h=120), TH).status == "warn"  # 80%
    assert health.check_budget(activity(scores_24h=151), TH).status == "fail"
    assert health.check_budget(activity(drafts_24h=16), TH).status == "fail"
    assert health.check_budget(activity(drafts_24h=12), TH).status == "warn"
    assert health.check_budget(StageActivity(), TH).status == "skip"


# ---- publish / feedback -------------------------------------------------------


def test_publish_partial_rows_fail():
    c = health.check_publish(PublishState(tables_present=True, partial_7d=1), NOW, TH)
    assert c.status == "fail" and "partial" in c.summary
    c = health.check_publish(PublishState(tables_present=True, failed_7d=2), NOW, TH)
    assert c.status == "fail"
    c = health.check_publish(PublishState(tables_present=True, stuck_claimed=1), NOW, TH)
    assert c.status == "fail" and "stuck" in c.summary
    c = health.check_publish(PublishState(tables_present=True, posts_24h=2, posts_7d=9), NOW, TH)
    assert c.status == "ok" and "2 posts" in c.summary


def test_publish_and_feedback_skip_without_tables():
    assert health.check_publish(PublishState(), NOW, TH).status == "skip"
    assert health.check_feedback(FeedbackState(), NOW, TH).status == "skip"


def test_feedback_snapshot_age():
    ok = FeedbackState(tables_present=True, latest_snapshot_on="2026-05-31")
    assert health.check_feedback(ok, NOW, TH).status == "ok"
    old = FeedbackState(tables_present=True, latest_snapshot_on="2026-05-01")
    assert health.check_feedback(old, NOW, TH).status == "warn"
    none = FeedbackState(tables_present=True)
    assert health.check_feedback(none, NOW, TH).status == "warn"


# ---- backups / storage / env --------------------------------------------------


def test_backups():
    assert health.check_backups(None, NOW, TH).status == "warn"
    fresh = (Path("pipeline-x.sqlite"), ago(10))
    assert health.check_backups(fresh, NOW, TH).status == "ok"
    old = (Path("pipeline-x.sqlite"), ago(37))
    c = health.check_backups(old, NOW, TH)
    assert c.status == "fail" and c.details["latest"] == "pipeline-x.sqlite"


def test_storage_thresholds():
    assert health.check_storage(10, 50_000, TH).status == "ok"
    assert health.check_storage(3000, 50_000, TH).status == "warn"
    assert health.check_storage(10, 500, TH).status == "fail"
    assert health.check_storage(3000, 500, TH).status == "fail"  # disk wins
    c = health.check_storage(None, None, TH, {"items": 3})
    assert c.status == "ok" and c.details["table_counts"] == {"items": 3}


def test_env_lists_missing_names_and_never_values():
    present = {"ANTHROPIC_API_KEY": True, "X_API_KEY": False}
    c = health.check_env(present, ("ANTHROPIC_API_KEY", "X_API_KEY", "OTHER"))
    assert c.status == "fail"
    assert c.details["missing"] == ["OTHER", "X_API_KEY"]
    assert "X_API_KEY" in c.summary
    assert health.check_env(present, ("ANTHROPIC_API_KEY",)).status == "ok"
    # The check only ever sees booleans, so no value can leak into the report.
    assert all(isinstance(v, bool) for v in present.values())


# ---- report -------------------------------------------------------------------


def test_skip_never_counts_against_overall():
    checks = [Check("a", "ok", ""), Check("b", "skip", "")]
    assert overall_status(checks) == "ok"
    assert overall_status([Check("a", "warn", ""), Check("b", "skip", "")]) == "warn"
    assert overall_status([Check("a", "warn", ""), Check("b", "fail", "")]) == "fail"
    assert overall_status([]) == "ok"
    assert overall_status([Check("a", "skip", "")]) == "ok"


def test_bad_status_rejected():
    with pytest.raises(ValueError):
        Check("a", "meh", "")


def test_run_all_and_format():
    report = health.run_all(
        now=NOW,
        thresholds=TH,
        source_runs=[SourceRun("a", last_run_at=ago(1))],
        activity=activity(),
        publish=PublishState(),
        feedback=FeedbackState(),
        latest_backup=(Path("pipeline-x.sqlite"), ago(1)),
        db_size_mb=1.0,
        disk_free_mb=10_000,
        env_present={"ANTHROPIC_API_KEY": True},
    )
    assert isinstance(report, Report)
    assert report.overall == "ok"
    names = [c.name for c in report.checks]
    assert names == [
        "env",
        "sources",
        "staleness",
        "backlog",
        "budget",
        "publish",
        "feedback",
        "backups",
        "storage",
    ]
    assert report.check("publish").status == "skip"
    text = health.format_report(report)
    assert "OK" in text.splitlines()[0]
    assert "[skip] publish" in text
    d = report.to_dict()
    assert d["overall"] == "ok" and d["checks"][0]["name"] == "env"


def test_thresholds_from_config_ignores_unknown_keys():
    th = Thresholds.from_config({"max_hours_since_ingest": 5, "bogus": 1, "required_env": ["A"]})
    assert th.max_hours_since_ingest == 5
    assert th.required_env == ("A",)
