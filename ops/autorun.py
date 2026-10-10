"""Automatic runs: which steps may run on their own and when (pure: no DB, no network, no
clock).

The control panel starts the configured steps at a few times of day while it is open
(`panel/autorun.py` owns the thread; the settings are the `auto_run_*` keys in
ops/config.yaml). Everything but publishing: a step may run automatically only when its
argv is `python <one of AUTO_SCRIPTS>`, whatever its name or the `auto_run_steps` list
says, so no spelling of a step can reach the publisher, run_ops.py or pipeline_cli.py.
The panel's JobManager re-checks this for every automatic run and also starts those runs
with posting switched off in their environment.

With `auto_run_backup_hours` set, an automatic run that starts when the newest database
backup is older than that also takes one (`backup_due`), so the day's first run backs up.

Times are wall-clock times of day in the display timezone (the root config.yaml
`timezone:`, read through timeutil), so they move with a DST change. At most MAX_TIMES a
day, at least MIN_SPACING_MINUTES apart, which bounds what unattended runs can cost.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from datetime import date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

# The only scripts an automatic run may launch. Publishing is not here and never will be:
# posting is manual only (the approved page's "Publish now").
AUTO_SCRIPTS = frozenset(
    {
        "run_ingest.py",
        "run_score.py",
        "run_feedback.py",
        "run_studio.py",
        "run_movers.py",
    }
)
# Belt and braces beside the allowlist: any argv element that starts like the publisher's
# live flag refuses the step (argparse would otherwise accept an abbreviation).
POSTING_PREFIX = "--liv"

MAX_TIMES = 8
MIN_SPACING_MINUTES = 60
DEFAULT_GRACE_MINUTES = 60

_TIME_RE = re.compile(r"^(\d{1,2}):(\d{2})$")


def posts_live(argv: Iterable[str]) -> bool:
    """Whether an argv carries the publisher's live flag, spelled out or abbreviated."""
    return any(str(a).startswith(POSTING_PREFIX) for a in argv)


def _one_time(item: Any) -> str:
    # PyYAML reads an unquoted 12:00 as the base-60 integer 720; turn it back into a time.
    if isinstance(item, int) and not isinstance(item, bool):
        if 0 <= item < 24 * 60:
            return f"{item // 60:02d}:{item % 60:02d}"
        raise ValueError(f"{item!r} is not a time of day (use HH:MM, e.g. 06:00)")
    text = str(item).strip()
    m = _TIME_RE.match(text)
    if not m or int(m.group(1)) > 23 or int(m.group(2)) > 59:
        raise ValueError(f"{text!r} is not a time of day (use HH:MM, e.g. 06:00)")
    return f"{int(m.group(1)):02d}:{m.group(2)}"


def _minutes(t: str) -> int:
    return int(t[:2]) * 60 + int(t[3:])


def parse_times(raw: str | Iterable[Any] | None) -> list[str]:
    """Run times as sorted, de-duplicated "HH:MM" strings. Takes the config list or the runs
    page's text field ("6:00, 12:00 18:00"). Raises ValueError for anything that is not a
    time of day, more than MAX_TIMES times, or two times closer than MIN_SPACING_MINUTES
    (measured round the clock, so 23:30 and 00:10 are 40 minutes apart)."""
    if raw is None:
        return []
    items = re.split(r"[,\s]+", raw) if isinstance(raw, str) else list(raw)
    times = sorted({_one_time(i) for i in items if str(i).strip()})
    if len(times) > MAX_TIMES:
        raise ValueError(f"at most {MAX_TIMES} times a day ({len(times)} given)")
    if len(times) > 1:
        mins = [_minutes(t) for t in times]
        gaps = [b - a for a, b in zip(mins, mins[1:], strict=False)] + [mins[0] + 1440 - mins[-1]]
        if min(gaps) < MIN_SPACING_MINUTES:
            raise ValueError(f"times must be at least {MIN_SPACING_MINUTES} minutes apart")
    return times


def settings_of(cfg: dict[str, Any]) -> dict[str, Any]:
    """The auto_run_* keys of an ops config, checked. A broken time list does not raise: it
    comes back as no times plus the error, so the runs page can say what is wrong."""
    error = None
    try:
        times = parse_times(cfg.get("auto_run_times"))
    except ValueError as exc:
        times, error = [], f"auto_run_times in ops/config.yaml: {exc}"
    try:
        grace = max(0, int(cfg.get("auto_run_grace_minutes", DEFAULT_GRACE_MINUTES)))
    except (TypeError, ValueError):
        grace = DEFAULT_GRACE_MINUTES
    try:
        backup_hours = max(0.0, float(cfg.get("auto_run_backup_hours", 0) or 0))
    except (TypeError, ValueError):
        backup_hours = 0.0
    steps = cfg.get("auto_run_steps") or []
    return {
        "enabled": bool(cfg.get("auto_run_enabled")),
        "times": times,
        "steps": [str(s) for s in steps] if isinstance(steps, list) else [],
        "grace_minutes": grace,
        "backup_hours": backup_hours,
        "error": error,
    }


def backup_due(latest: datetime | None, now: datetime, hours: float) -> bool:
    """Whether an automatic run should also back the database up: on (`hours` > 0) and the
    newest backup is older than `hours`, or there is none."""
    if hours <= 0:
        return False
    return latest is None or (now - latest).total_seconds() > hours * 3600


def ineligible(step: Any) -> str | None:
    """Why a configured step may not run automatically, or None when it may. `step` is an
    ops.runner.Step (anything with `argv`)."""
    argv = [str(a) for a in getattr(step, "argv", [])]
    if posts_live(argv):
        return "it carries the publisher's live flag"
    if getattr(step, "manual", False):
        return "it is a manual step (a button on its page)"
    if len(argv) < 2 or argv[0] != "python" or argv[1] not in AUTO_SCRIPTS:
        return "only ingest, score, movers, feedback and studio run automatically"
    return None


def plan(names: list[str], steps: list[Any]) -> tuple[list[str], dict[str, str]]:
    """Split the configured step names into those an automatic run starts (in the order of
    `steps`, the config's order) and those it leaves out, with the reason for each."""
    by_name = {s.name: s for s in steps}
    dropped: dict[str, str] = {}
    for name in names:
        if name not in by_name:
            dropped[name] = "no such step in ops/config.yaml (restart the app after editing it)"
        elif (why := ineligible(by_name[name])) is not None:
            dropped[name] = why
    keep = [s.name for s in steps if s.name in names and s.name not in dropped]
    return keep, dropped


def _slots_on(day: date, times: list[str], tz: ZoneInfo) -> list[datetime]:
    # fold=0: on the night clocks go back, a time in the repeated hour is its first
    # occurrence, so it still runs once.
    return [datetime.combine(day, time(int(t[:2]), int(t[3:])), tzinfo=tz) for t in times]


def slots_between(last: datetime, now: datetime, times: list[str], tz: ZoneInfo) -> list[datetime]:
    """Every run time in (last, now], oldest first. Empty when the clock went backwards."""
    if now <= last or not times:
        return []
    out: list[datetime] = []
    day = last.astimezone(tz).date() - timedelta(days=1)
    end = now.astimezone(tz).date()
    while day <= end:
        out += [s for s in _slots_on(day, times, tz) if last < s <= now]
        day += timedelta(days=1)
    return sorted(out)


def next_slot(now: datetime, times: list[str], tz: ZoneInfo) -> datetime | None:
    """The first run time strictly after `now`, or None with no times."""
    if not times:
        return None
    day = now.astimezone(tz).date()
    for offset in range(3):
        later = [s for s in _slots_on(day + timedelta(days=offset), times, tz) if s > now]
        if later:
            return min(later)
    return None
