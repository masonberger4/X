"""Pure health checks over the dataclasses in ops/models.py.

No DB, no network, no clock reads: `now` is always a parameter. Each check returns a
Check(name, status, summary, details). Summaries and details name checks, sources,
counts and ages only; never post text, abstracts, or secret values.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from ops.models import (
    STATUS_FAIL,
    STATUS_OK,
    STATUS_SKIP,
    STATUS_WARN,
    Check,
    FeedbackState,
    PublishState,
    Report,
    SourceRun,
    StageActivity,
)


@dataclass
class Thresholds:
    max_hours_since_ingest: float = 3
    max_hours_since_score: float = 6
    max_hours_since_draft: float = 48
    source_stale_multiplier: float = 3
    max_source_failure_fraction: float = 0.34
    pending_draft_max_age_hours: float = 72
    unscored_backlog_max: int = 100
    scores_per_day_max: int = 150
    drafts_per_day_max: int = 15
    db_max_mb: float = 2048
    disk_min_free_mb: float = 1024
    backup_max_age_hours: float = 36
    feedback_max_age_hours: float = 192
    required_env: tuple[str, ...] = ("ANTHROPIC_API_KEY",)

    @classmethod
    def from_config(cls, raw: dict[str, Any] | None) -> Thresholds:
        raw = dict(raw or {})
        if "required_env" in raw:
            raw["required_env"] = tuple(str(n) for n in raw["required_env"] or ())
        known = {k: v for k, v in raw.items() if k in cls.__dataclass_fields__}
        return cls(**known)


def hours_between(now: datetime, then: datetime | None) -> float | None:
    if then is None:
        return None
    return (now - then).total_seconds() / 3600.0


def _fmt_hours(h: float | None) -> str:
    if h is None:
        return "never"
    if h < 1:
        return f"{h * 60:.0f}m"
    if h < 48:
        return f"{h:.1f}h"
    return f"{h / 24:.1f}d"


# ---------------------------------------------------------------------------
# Individual checks
# ---------------------------------------------------------------------------


def check_sources(runs: list[SourceRun], now: datetime, th: Thresholds) -> Check:
    """Each enabled source: ok / error / never ran / stale (last run > multiplier x cadence)."""
    enabled = [r for r in runs if r.enabled]
    if not enabled:
        return Check("sources", STATUS_SKIP, "no enabled sources configured", {})
    errors: list[str] = []
    never: list[str] = []
    stale: list[str] = []
    for r in enabled:
        if r.last_run_at is None:
            never.append(r.source)
            continue
        if r.error:
            errors.append(r.source)
            continue
        age_h = hours_between(now, r.last_run_at)
        limit_h = th.source_stale_multiplier * r.cadence_minutes / 60.0
        if age_h is not None and age_h > limit_h:
            stale.append(r.source)
    bad = len(errors) + len(never) + len(stale)
    fraction = bad / len(enabled)
    details = {
        "enabled": len(enabled),
        "failing": bad,
        "failure_fraction": round(fraction, 3),
        "error": sorted(errors),
        "never_ran": sorted(never),
        "stale": sorted(stale),
    }
    if bad == 0:
        return Check("sources", STATUS_OK, f"{len(enabled)} sources ok", details)
    summary = (
        f"{bad} of {len(enabled)} sources need attention "
        f"({len(errors)} error, {len(never)} never ran, {len(stale)} stale)"
    )
    status = STATUS_FAIL if fraction >= th.max_source_failure_fraction else STATUS_WARN
    return Check("sources", status, summary, details)


def _tiered(age_h: float | None, limit_h: float) -> str:
    """ok within limit, warn past it, fail past twice it (or when it never happened)."""
    if age_h is None:
        return STATUS_FAIL
    if age_h > 2 * limit_h:
        return STATUS_FAIL
    if age_h > limit_h:
        return STATUS_WARN
    return STATUS_OK


def check_staleness(activity: StageActivity, now: datetime, th: Thresholds) -> Check:
    if not activity.tables_present:
        return Check("staleness", STATUS_SKIP, "pipeline tables not present yet", {})
    parts = {
        "ingest": (hours_between(now, activity.latest_fetched_at), th.max_hours_since_ingest),
        "score": (hours_between(now, activity.latest_scored_at), th.max_hours_since_score),
        "draft": (hours_between(now, activity.latest_draft_at), th.max_hours_since_draft),
    }
    statuses = {name: _tiered(age, limit) for name, (age, limit) in parts.items()}
    # Scoring/drafting lag is only a problem when there is something to score/draft;
    # a quiet pipeline with nothing crossing the threshold is not stale.
    if activity.unscored_backlog == 0 and statuses["score"] != STATUS_OK:
        statuses["score"] = STATUS_OK if activity.latest_scored_at else STATUS_WARN
    details = {
        name: {"hours_since": None if age is None else round(age, 2), "max_hours": limit}
        for name, (age, limit) in parts.items()
    }
    details["statuses"] = statuses
    worst = max(statuses.values(), key=lambda s: {"ok": 0, "warn": 1, "fail": 2}[s])
    summary = ", ".join(f"{n} {_fmt_hours(parts[n][0])} ago" for n in parts)
    if worst != STATUS_OK:
        late = [n for n, s in statuses.items() if s != STATUS_OK]
        summary = f"{'/'.join(late)} stale: " + summary
    return Check("staleness", worst, summary, details)


def check_backlog(activity: StageActivity, now: datetime, th: Thresholds) -> Check:
    if not activity.tables_present:
        return Check("backlog", STATUS_SKIP, "pipeline tables not present yet", {})
    status = STATUS_OK
    notes: list[str] = []
    if activity.unscored_backlog > 2 * th.unscored_backlog_max:
        status = STATUS_FAIL
        notes.append(f"{activity.unscored_backlog} unscored clusters")
    elif activity.unscored_backlog > th.unscored_backlog_max:
        status = STATUS_WARN
        notes.append(f"{activity.unscored_backlog} unscored clusters")
    oldest = activity.oldest_pending_age_hours
    if oldest is not None and oldest > th.pending_draft_max_age_hours:
        status = STATUS_WARN if status == STATUS_OK else status
        notes.append(f"oldest pending draft {_fmt_hours(oldest)} old")
    details = {
        "unscored_backlog": activity.unscored_backlog,
        "unscored_backlog_max": th.unscored_backlog_max,
        "pending_drafts": activity.pending_drafts,
        "oldest_pending_age_hours": None if oldest is None else round(oldest, 2),
        "pending_draft_max_age_hours": th.pending_draft_max_age_hours,
        "approved_drafts": activity.approved_drafts,
    }
    if status == STATUS_OK:
        summary = (
            f"{activity.unscored_backlog} unscored, {activity.pending_drafts} pending, "
            f"{activity.approved_drafts} approved"
        )
    else:
        summary = "; ".join(notes)
    return Check("backlog", status, summary, details)


def check_budget(activity: StageActivity, th: Thresholds) -> Check:
    if not activity.tables_present:
        return Check("budget", STATUS_SKIP, "pipeline tables not present yet", {})
    status = STATUS_OK
    notes: list[str] = []
    for label, used, cap in (
        ("scores", activity.scores_24h, th.scores_per_day_max),
        ("drafts", activity.drafts_24h, th.drafts_per_day_max),
    ):
        if cap <= 0:
            continue
        if used > cap:
            status = STATUS_FAIL
            notes.append(f"{label} {used}/{cap} in 24h (over cap)")
        elif used >= 0.8 * cap:
            status = STATUS_WARN if status == STATUS_OK else status
            notes.append(f"{label} {used}/{cap} in 24h (near cap)")
    details = {
        "scores_24h": activity.scores_24h,
        "scores_per_day_max": th.scores_per_day_max,
        "drafts_24h": activity.drafts_24h,
        "drafts_per_day_max": th.drafts_per_day_max,
    }
    summary = (
        "; ".join(notes)
        if notes
        else f"{activity.scores_24h} scores, {activity.drafts_24h} drafts in 24h"
    )
    return Check("budget", status, summary, details)


def check_publish(state: PublishState, now: datetime, th: Thresholds) -> Check:
    if not state.tables_present:
        return Check("publish", STATUS_SKIP, "publish tables not present yet", {})
    details = {
        "posts_24h": state.posts_24h,
        "posts_7d": state.posts_7d,
        "partial_7d": state.partial_7d,
        "failed_7d": state.failed_7d,
        "stuck_claimed": state.stuck_claimed,
        "hours_since_last_post": _round(hours_between(now, state.latest_posted_at)),
    }
    problems: list[str] = []
    if state.partial_7d:
        problems.append(f"{state.partial_7d} partial thread(s) need a human")
    if state.failed_7d:
        problems.append(f"{state.failed_7d} failed post(s) need a human")
    if state.stuck_claimed:
        problems.append(f"{state.stuck_claimed} claim(s) stuck >1h (crashed run?)")
    if problems:
        return Check("publish", STATUS_FAIL, "; ".join(problems), details)
    summary = f"{state.posts_24h} posts in 24h, {state.posts_7d} in 7d"
    return Check("publish", STATUS_OK, summary, details)


def check_feedback(state: FeedbackState, now: datetime, th: Thresholds) -> Check:
    if not state.tables_present:
        return Check("feedback", STATUS_SKIP, "feedback tables not present yet", {})
    details = {
        "latest_captured_on": state.latest_captured_on,
        "latest_snapshot_on": state.latest_snapshot_on,
        "latest_report_at": state.latest_report_at,
    }
    if state.latest_snapshot_on is None:
        return Check("feedback", STATUS_WARN, "no follower snapshot yet", details)
    try:
        snap = datetime.fromisoformat(state.latest_snapshot_on).replace(tzinfo=now.tzinfo)
    except ValueError:
        return Check("feedback", STATUS_WARN, "unparsable snapshot date", details)
    age = hours_between(now, snap)
    if age is not None and age > th.feedback_max_age_hours:
        return Check(
            "feedback", STATUS_WARN, f"last follower snapshot {_fmt_hours(age)} ago", details
        )
    return Check("feedback", STATUS_OK, f"snapshot {state.latest_snapshot_on}", details)


def check_backups(latest: tuple[Path, datetime] | None, now: datetime, th: Thresholds) -> Check:
    if latest is None:
        return Check("backups", STATUS_WARN, "no backup yet", {"latest": None})
    path, when = latest
    age = hours_between(now, when)
    details = {
        "latest": path.name,
        "age_hours": _round(age),
        "max_age_hours": th.backup_max_age_hours,
    }
    if age is not None and age > th.backup_max_age_hours:
        return Check("backups", STATUS_FAIL, f"latest backup {_fmt_hours(age)} old", details)
    return Check("backups", STATUS_OK, f"latest backup {_fmt_hours(age)} old", details)


def check_storage(
    db_size_mb: float | None,
    disk_free_mb: float | None,
    th: Thresholds,
    table_counts: dict[str, int] | None = None,
) -> Check:
    details: dict[str, Any] = {
        "db_size_mb": _round(db_size_mb),
        "db_max_mb": th.db_max_mb,
        "disk_free_mb": _round(disk_free_mb),
        "disk_min_free_mb": th.disk_min_free_mb,
    }
    if table_counts:
        details["table_counts"] = dict(table_counts)
    if disk_free_mb is not None and disk_free_mb < th.disk_min_free_mb:
        return Check("storage", STATUS_FAIL, f"disk free {disk_free_mb:.0f} MB", details)
    if db_size_mb is not None and db_size_mb > th.db_max_mb:
        return Check("storage", STATUS_WARN, f"db size {db_size_mb:.0f} MB", details)
    size = "?" if db_size_mb is None else f"{db_size_mb:.1f}"
    free = "?" if disk_free_mb is None else f"{disk_free_mb:.0f}"
    return Check("storage", STATUS_OK, f"db {size} MB, disk free {free} MB", details)


def check_env(present: dict[str, bool], required: tuple[str, ...] | list[str]) -> Check:
    """`present` maps env var name -> whether it is set and non-empty. Values are never seen."""
    missing = sorted(n for n in required if not present.get(n, False))
    details = {"required": list(required), "missing": missing}
    if missing:
        return Check("env", STATUS_FAIL, f"missing env: {', '.join(missing)}", details)
    return Check("env", STATUS_OK, f"{len(required)} required env vars set", details)


def _round(x: float | None, nd: int = 2) -> float | None:
    return None if x is None else round(x, nd)


# ---------------------------------------------------------------------------
# All together
# ---------------------------------------------------------------------------


def run_all(
    *,
    now: datetime,
    thresholds: Thresholds,
    source_runs: list[SourceRun],
    activity: StageActivity,
    publish: PublishState,
    feedback: FeedbackState,
    latest_backup: tuple[Path, datetime] | None,
    db_size_mb: float | None,
    disk_free_mb: float | None,
    env_present: dict[str, bool],
    table_counts: dict[str, int] | None = None,
) -> Report:
    checks = [
        check_env(env_present, thresholds.required_env),
        check_sources(source_runs, now, thresholds),
        check_staleness(activity, now, thresholds),
        check_backlog(activity, now, thresholds),
        check_budget(activity, thresholds),
        check_publish(publish, now, thresholds),
        check_feedback(feedback, now, thresholds),
        check_backups(latest_backup, now, thresholds),
        check_storage(db_size_mb, disk_free_mb, thresholds, table_counts),
    ]
    return Report.build(now, checks)


def format_report(report: Report) -> str:
    """Plain-text rendering for `run_ops.py health`."""
    lines = [f"Pipeline health at {report.checked_at.isoformat()}: {report.overall.upper()}"]
    for c in report.checks:
        lines.append(f"  [{c.status:<4}] {c.name:<10} {c.summary}")
    return "\n".join(lines)
