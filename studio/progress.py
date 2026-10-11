"""A piece's progress, as the studio pages show it while a session runs (pure: no DB, no
network, no clock; `now` is a parameter).

A piece goes research -> write (the post, the cards and the cold fact-check) -> polish ->
the queue. How far along the running stage is can only be estimated: a session decides for
itself when it is done. The estimate is the time spent in the stage against how long that
stage usually takes on this account (the median of its finished runs), never past
ESTIMATE_CAP of it, so the bar does not reach the end before the stage does.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from statistics import median

from studio import store as S

STEPS = (
    ("research", "Research"),
    ("write", "Write + fact-check"),
    ("polish", "Polish"),
    ("queue", "In the queue"),
)
# Minutes a stage takes when the account has no finished run of it yet.
DEFAULT_MINUTES = {"research": 45.0, "write": 45.0, "polish": 15.0, "revise": 30.0}
ESTIMATE_CAP = 0.95
WAITING = "Waiting for you to read the research"
TYPICAL_RUNS = 20  # the latest finished runs per stage the typical time is taken from
_AT = {
    S.STAGE_RESEARCHING: 0,
    S.STAGE_RESEARCH_READY: 1,
    S.STAGE_WRITING: 1,
    S.STAGE_REVISING: 1,
    S.STAGE_POLISHING: 2,
    S.STAGE_READY: 3,
}
_RUN_STAGE = {
    S.STAGE_RESEARCHING: "research",
    S.STAGE_WRITING: "write",
    S.STAGE_REVISING: "revise",
    S.STAGE_POLISHING: "polish",
}


@dataclass
class Step:
    label: str
    state: str  # done | now | todo


@dataclass
class Progress:
    steps: list[Step]
    percent: int  # the whole piece, estimated
    stage_label: str
    minutes: float  # in the running stage so far
    typical: float  # how long that stage usually takes
    doing: str  # the session's latest log line


def typical_minutes(runs: Sequence[S.RunRow]) -> dict[str, float]:
    """Per stage, the median minutes of its finished `ok` runs, latest TYPICAL_RUNS of each."""
    by: dict[str, list[float]] = {}
    for r in sorted(runs, key=lambda r: r.id, reverse=True):
        if r.outcome == "ok" and r.duration_ms > 0 and len(by.get(r.stage, [])) < TYPICAL_RUNS:
            by.setdefault(r.stage, []).append(r.duration_ms / 60000)
    return {stage: median(values) for stage, values in by.items()}


def _parse(stamp: str) -> datetime | None:
    try:
        return datetime.fromisoformat(stamp)
    except (TypeError, ValueError):
        return None


def progress(
    stage: str,
    runs: Sequence[S.RunRow],
    typical: dict[str, float],
    now: datetime,
    log_tail: str = "",
) -> Progress | None:
    """The piece's progress, or None for a piece that is not on its way (failed, stopped,
    discarded). `runs` are the piece's own, oldest first."""
    at = _AT.get(stage)
    if at is None:
        return None
    steps = [
        Step(label, "done" if i < at or stage == S.STAGE_READY else ("now" if i == at else "todo"))
        for i, (_, label) in enumerate(STEPS)
    ]
    run_stage = _RUN_STAGE.get(stage, "")
    minutes = 0.0
    if run_stage:
        open_run = next(
            (r for r in reversed(runs) if r.stage == run_stage and not r.finished_at), None
        )
        started = _parse(open_run.started_at) if open_run else None
        if started is not None:
            minutes = max(0.0, (now - started).total_seconds() / 60)

    def usual(key: str) -> float:
        return float(typical.get(key) or DEFAULT_MINUTES.get(key, 30.0))

    plan = ["research", "write", "polish"]
    total = sum(usual(k) for k in plan)
    if stage == S.STAGE_READY:
        percent = 100
    else:
        done = sum(usual(k) for k in plan[: min(at, 3)])
        if run_stage:
            # the share of the running stage gone by, of the plan's step it stands in
            share = min(minutes / usual(run_stage), ESTIMATE_CAP)
            done += share * usual(plan[min(at, 2)])
        percent = int(round(100 * done / total)) if total else 0
    lines = [line.strip() for line in log_tail.splitlines() if line.strip()]
    return Progress(
        steps=steps,
        percent=max(0, min(100, percent)),
        stage_label=WAITING if stage == S.STAGE_RESEARCH_READY else STEPS[at][1],
        minutes=minutes,
        typical=usual(run_stage) if run_stage else 0.0,
        doing=lines[-1][:200] if lines else "",
    )
