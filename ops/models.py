"""Dataclasses shared by the read-only adapters (ops/store.py) and the pure health checks.

Everything here is plain data with no DB, network, or clock access so health.py can be
unit-tested with hand-built instances.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any

STATUS_OK = "ok"
STATUS_WARN = "warn"
STATUS_FAIL = "fail"
STATUS_SKIP = "skip"
STATUSES = (STATUS_OK, STATUS_WARN, STATUS_FAIL, STATUS_SKIP)

# Worst-first ordering used to compute a report's overall status. 'skip' never counts.
_SEVERITY = {STATUS_FAIL: 3, STATUS_WARN: 2, STATUS_OK: 1, STATUS_SKIP: 0}


@dataclass
class SourceRun:
    """One configured ingest source joined with its (optional) source_runs row."""

    source: str
    enabled: bool = True
    cadence_minutes: int = 60
    last_run_at: datetime | None = None  # None == never ran
    fetched: int = 0
    inserted: int = 0
    error: str | None = None


@dataclass
class StageActivity:
    """Latest activity and backlog across step 1 (ingest/score) and step 2 (draft/queue)."""

    tables_present: bool = False
    latest_fetched_at: datetime | None = None
    latest_scored_at: datetime | None = None
    latest_draft_at: datetime | None = None
    latest_decision_at: datetime | None = None
    unscored_backlog: int = 0  # prefilter-passed clusters with no score row
    pending_drafts: int = 0
    oldest_pending_age_hours: float | None = None
    approved_drafts: int = 0
    scores_24h: int = 0
    drafts_24h: int = 0


@dataclass
class PublishState:
    """Step 3's schedule/posts tables, summarised."""

    tables_present: bool = False
    posts_24h: int = 0
    posts_7d: int = 0
    partial_7d: int = 0  # schedule rows 'partial' in the last 7 days (need a human)
    failed_7d: int = 0  # schedule rows 'failed' in the last 7 days (need a human)
    stuck_claimed: int = 0  # 'claimed' for more than an hour (a crashed run)
    latest_posted_at: datetime | None = None


@dataclass
class FeedbackState:
    """Step 4's metrics/snapshots/reports tables, summarised."""

    tables_present: bool = False
    latest_captured_on: str | None = None  # YYYY-MM-DD
    latest_snapshot_on: str | None = None  # YYYY-MM-DD
    latest_report_at: datetime | None = None


@dataclass
class Check:
    name: str
    status: str
    summary: str
    details: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.status not in STATUSES:
            raise ValueError(f"bad check status {self.status!r}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "status": self.status,
            "summary": self.summary,
            "details": _jsonable(self.details),
        }


@dataclass
class Report:
    checked_at: datetime
    overall: str
    checks: list[Check]

    @classmethod
    def build(cls, checked_at: datetime, checks: list[Check]) -> Report:
        return cls(checked_at=checked_at, overall=overall_status(checks), checks=list(checks))

    def check(self, name: str) -> Check | None:
        return next((c for c in self.checks if c.name == name), None)

    def to_dict(self) -> dict[str, Any]:
        return {
            "checked_at": self.checked_at.isoformat(),
            "overall": self.overall,
            "checks": [c.to_dict() for c in self.checks],
        }


def overall_status(checks: list[Check]) -> str:
    """Worst status among the checks; 'skip' never counts. No checks at all -> 'ok'."""
    worst = STATUS_OK
    for c in checks:
        if c.status == STATUS_SKIP:
            continue
        if _SEVERITY[c.status] > _SEVERITY[worst]:
            worst = c.status
    return worst


def _jsonable(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {str(k): _jsonable(v) for k, v in obj.items()}
    if isinstance(obj, list | tuple):
        return [_jsonable(v) for v in obj]
    if isinstance(obj, datetime):
        return obj.isoformat()
    if hasattr(obj, "__dataclass_fields__"):
        return _jsonable(asdict(obj))
    return obj
