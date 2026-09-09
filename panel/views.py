"""Pure view models for the control panel templates.

No database, no network, no clock reads: `now` is always a parameter, so every
function here is unit-testable with hand-built inputs. Nothing in this module renders
post text, abstracts or secret values — it formats check summaries, step outcomes,
ages and counts.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from ops.models import Report, SourceRun

# Worst first: problems at the top, then healthy rows, then anything not applicable.
_SEVERITY = {"fail": 0, "warn": 1, "ok": 2, "skip": 3}


def fmt_age(now: datetime, then: datetime | None) -> str:
    """Human age of a timestamp: 'never', '40m ago', '3.2h ago', '2.1d ago'."""
    if then is None:
        return "never"
    return fmt_hours((now - then).total_seconds() / 3600.0) + " ago"


def fmt_hours(hours: float | None) -> str:
    if hours is None:
        return "never"
    if hours < 1:
        return f"{hours * 60:.0f}m"
    if hours < 48:
        return f"{hours:.1f}h"
    return f"{hours / 24:.1f}d"


def fmt_duration(seconds: float | None) -> str:
    if seconds is None:
        return ""
    if seconds < 60:
        return f"{seconds:.0f}s"
    return f"{seconds / 60:.1f}m"


def check_rows(report: Report | None) -> list[dict[str, Any]]:
    """One row per health check, worst status first."""
    if report is None:
        return []
    rows = [
        {
            "name": c.name,
            "status": c.status,
            "summary": c.summary,
            "details": c.details,
        }
        for c in report.checks
    ]
    rows.sort(key=lambda r: (_SEVERITY.get(r["status"], 9), r["name"]))
    return rows


def outcome_of(run: dict[str, Any] | None) -> tuple[str, str]:
    """(status, label) for a pipeline_runs row: the same vocabulary as the health pills."""
    if run is None:
        return "skip", "never run"
    if run.get("skipped_reason"):
        return "skip", f"skipped ({run['skipped_reason']})"
    if run.get("timed_out"):
        return "fail", "timed out"
    if run.get("exit_code") == 0:
        return "ok", "exit 0"
    return "fail", f"exit {run.get('exit_code')}"


def step_rows(
    steps: list[Any], last_runs: dict[str, dict[str, Any]], now: datetime
) -> list[dict[str, Any]]:
    """One row per configured orchestrator step, with its last recorded outcome.

    `steps` are ops.runner.Step instances; `last_runs` is ops.store.last_run_per_step.
    A disabled step is shown as such and cannot be started from the panel — the runner
    skips it, which is what keeps publishing off by default.
    """
    rows = []
    for step in steps:
        run = last_runs.get(step.name)
        status, label = outcome_of(run)
        rows.append(
            {
                "name": step.name,
                "argv": " ".join(step.argv),
                "enabled": step.enabled,
                "required": step.required,
                "timeout_seconds": step.timeout_seconds,
                "status": status,
                "outcome": label,
                "age": fmt_age(now, run["finished_at"]) if run else "never",
                "finished_at": run["finished_at"] if run else None,
            }
        )
    return rows


def source_rows(runs: list[SourceRun], now: datetime, stale_multiplier: float) -> list[dict]:
    """One row per configured ingest source, worst first (error, never ran, stale, ok)."""
    rows = []
    for r in runs:
        if not r.enabled:
            status = "skip"
        elif r.error:
            status = "fail"
        elif r.last_run_at is None:
            status = "warn"
        else:
            age_h = (now - r.last_run_at).total_seconds() / 3600.0
            limit_h = stale_multiplier * r.cadence_minutes / 60.0
            status = "warn" if age_h > limit_h else "ok"
        rows.append(
            {
                "source": r.source,
                "enabled": r.enabled,
                "cadence_minutes": r.cadence_minutes,
                "age": fmt_age(now, r.last_run_at),
                "fetched": r.fetched,
                "inserted": r.inserted,
                "error": r.error,
                "status": status,
            }
        )
    rows.sort(key=lambda r: (_SEVERITY.get(r["status"], 9), r["source"]))
    return rows


# ---------------------------------------------------------------------------
# follower trend
# ---------------------------------------------------------------------------

SPARK_WIDTH = 640
SPARK_HEIGHT = 90
SPARK_PAD = 6


def sparkline(values: list[int], width: int = SPARK_WIDTH, height: int = SPARK_HEIGHT) -> dict:
    """Geometry for one series over time. Returns coordinates, never markup.

    One series, so the template draws a single line with no legend and labels only the
    first and last point. A flat series is centred rather than divided by a zero range.
    """
    if len(values) < 2:
        return {}
    lo, hi = min(values), max(values)
    span = hi - lo
    inner_h = height - 2 * SPARK_PAD
    step = (width - 2 * SPARK_PAD) / (len(values) - 1)
    points = []
    for i, v in enumerate(values):
        x = SPARK_PAD + i * step
        y = SPARK_PAD + inner_h / 2 if span == 0 else SPARK_PAD + inner_h * (1 - (v - lo) / span)
        points.append((round(x, 1), round(y, 1)))
    return {
        "points": " ".join(f"{x},{y}" for x, y in points),
        "last": points[-1],
        "first": points[0],
        "min": lo,
        "max": hi,
        "width": width,
        "height": height,
    }


def series_growth(series: list[dict]) -> dict:
    """Followers now, and the change over the window the snapshots cover."""
    if not series:
        return {}
    first, last = series[0], series[-1]
    change = last["followers"] - first["followers"]
    return {
        "followers": last["followers"],
        "change": change,
        "sign": "+" if change > 0 else "",
        "days": len(series),
        "since": first["captured_on"],
        "on": last["captured_on"],
    }
