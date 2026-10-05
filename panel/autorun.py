"""Automatic runs while the control panel is open: everything but publishing.

A background thread wakes every `poll_seconds` and, at each time of day in
`auto_run_times` (ops/config.yaml, the display timezone), asks the JobManager for one
automatic run of `auto_run_steps`. Which steps may run, and how times are read, is the pure
`ops/autorun.py`; this module only keeps the clock and the bookkeeping.

Posting stays manual only. The steps go through `JobManager.start(..., auto=True)`, which
refuses any step that is not one of ingest, score, draft, verify, feedback or evolve (by
the script it runs, not by its name) and runs the rest with posting switched off in their
environment. This module never touches "Publish now".

When a run time fires:
- Only times that pass while this runner is on and leading count. The watermark `last`
  moves forward on every tick whatever happens, so switching the runs on, adding a time
  that is already past today, another window taking over, or the clock going back never
  starts a run nobody scheduled, and a time that passed while the app was closed is not
  made up.
- A time is run at most `auto_run_grace_minutes` late. One that passed while the PC slept
  runs on wake if it is still within that; several missed at once run once, as the latest.
- If a step of the run is still going in this window (the previous automatic run, or one a
  human started), the time waits for it, retried every tick, until the grace runs out.
- With several panel windows open, one runs the timer: it holds `<lock_path>.autorun`
  (the OS lock alone decides, so a crash never leaves it stuck); the others show who leads.
- Daily backup: when a run starts and the newest backup is older than
  `auto_run_backup_hours`, the database is backed up (`backup`, the dashboard's "Back up
  now"). It runs in the timer thread after the run has started, outside the lock the pages
  read the status under, so a page never waits for it.

`tick(now)` is the whole decision and takes the clock as a parameter, so tests drive it
without the thread. The app starts and stops the thread in its lifespan (`panel/app.py`),
and closing the desktop window stops it before in-flight runs are cancelled
(`run_desktop.stop_run`), so no new run can start after the window closed.
"""

from __future__ import annotations

import logging
import threading
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import timeutil
from ops import lock
from ops.autorun import backup_due, next_slot, plan, settings_of, slots_between
from ops.config import load_ops_config
from panel.jobs import JobError, JobManager

log = logging.getLogger(__name__)

POLL_SECONDS = 30.0
LEADER_SUFFIX = ".autorun"  # the leader lock: <lock_path>.autorun
HISTORY = 10  # run times remembered for the runs page

REASON_OFF = "off"
REASON_NO_TIMES = "on, but no run times are set"
REASON_FOLLOWER = "another panel window runs the automatic runs"
REASON_IDLE = "waiting for the next run time"
REASON_STOPPED = "stopped (the app is closing)"


@dataclass
class Outcome:
    """What happened to one run time."""

    slot: datetime
    at: datetime
    text: str
    job_id: str | None = None


@dataclass
class AutoStatus:
    """What the runs page and the dashboard show about the timer."""

    enabled: bool
    times: list[str]
    steps: list[str]  # what a run would start
    dropped: dict[str, str]  # configured steps it leaves out, and why
    grace_minutes: int
    backup_hours: float  # 0: no daily backup
    error: str | None
    next_due: datetime | None
    pending: datetime | None  # a run time waiting for a busy step
    leader: bool
    holder: dict[str, Any] | None  # the leading process, when it is not this one
    reason: str
    outcomes: list[Outcome] = field(default_factory=list)


class AutoRunner:
    def __init__(
        self,
        jobs: JobManager,
        lock_path: str | Path,
        settings: Callable[[], dict[str, Any]] = load_ops_config,
        tz: Callable[[], ZoneInfo] = timeutil.display_tz,
        poll_seconds: float = POLL_SECONDS,
        backup: Callable[[datetime], Path] | None = None,
        latest_backup: Callable[[], datetime | None] | None = None,
    ) -> None:
        self.jobs = jobs
        self._backup = backup
        self._latest_backup = latest_backup
        self._backup_wanted = False
        self.lock_path = Path(lock_path)
        self._settings = settings
        self._tz = tz
        self.poll_seconds = poll_seconds
        self.last: datetime | None = None  # the watermark: no run time at or before it fires
        self.pending: datetime | None = None
        self.wait_reason: str | None = None
        self.reason = REASON_OFF
        self.outcomes: deque[Outcome] = deque(maxlen=HISTORY)
        self._held: lock.Lock | None = None
        self._mutex = threading.Lock()
        self._stopping = False
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # -- the decision ------------------------------------------------------

    def tick(self, now: datetime | None = None) -> str:
        """Start a run if one is due; returns why it did or did not."""
        now = now or datetime.now(UTC)
        with self._mutex:
            if self._stopping:
                self.reason = REASON_STOPPED
                return self.reason
            try:
                self.reason = self._decide(now)
            finally:
                self.last = now if self.last is None else max(self.last, now)
            reason = self.reason
            wanted, self._backup_wanted = self._backup_wanted, False
        if wanted:
            self._run_backup(now)
        return reason

    def _run_backup(self, now: datetime) -> None:
        """The daily backup, outside the mutex (it takes seconds). Never raises: a failure is
        logged and shown, and the health check's backup age flags it too."""
        try:
            path = self._backup(now)  # type: ignore[misc]
            text = f"backup saved: {Path(path).name}"
        except Exception as exc:
            log.exception("automatic runs: the daily backup failed")
            text = f"backup failed: {exc}"
        with self._mutex:
            self.outcomes.appendleft(Outcome(slot=now, at=now, text=text))

    def _decide(self, now: datetime) -> str:
        if self.last is None:
            self.last = now  # the first tick: nothing before this moment is made up
        cfg = settings_of(self._settings())
        if not cfg["enabled"]:
            self.pending = self.wait_reason = None
            self._release()
            return REASON_OFF
        if cfg["error"]:
            self.pending = self.wait_reason = None
            return cfg["error"]
        if not cfg["times"]:
            self.pending = self.wait_reason = None
            return REASON_NO_TIMES
        if not self._lead():
            self.pending = self.wait_reason = None
            return REASON_FOLLOWER

        due = slots_between(self.last, now, cfg["times"], self._tz())
        if due:
            if self.pending is not None:
                why = self.wait_reason or "the next run time came first"
                self._note(self.pending, now, f"skipped: {why}")
            for older in due[:-1]:
                self._note(older, now, "skipped: missed while the computer was asleep")
            self.pending, self.wait_reason = due[-1], None
        if self.pending is None:
            return REASON_IDLE

        grace = cfg["grace_minutes"]
        if (now - self.pending).total_seconds() > grace * 60:
            why = self.wait_reason or f"more than {grace} minutes late (the computer was asleep)"
            self._note(self.pending, now, f"skipped: {why}")
            self.pending = self.wait_reason = None
            return REASON_IDLE

        names, dropped = plan(cfg["steps"], self.jobs.steps())
        if not names:
            self._note(self.pending, now, "skipped: no steps that can run automatically")
            self.pending = self.wait_reason = None
            return REASON_IDLE
        busy = sorted(set(names) & self.jobs.busy_steps())
        # A step marked skip_when_busy (the studio) sits this slot out rather than holding
        # every other step back until it finishes.
        skippable = {s.name for s in self.jobs.steps() if s.skip_when_busy}
        for name in [b for b in busy if b in skippable]:
            names = [n for n in names if n != name]
            dropped[name] = "still running from an earlier run"
        busy = [b for b in busy if b not in skippable]
        if busy:
            self.wait_reason = f"{', '.join(busy)} still running"
            return f"waiting: {self.wait_reason}"
        if not names:
            self._note(self.pending, now, "skipped: every step is still running")
            self.pending = self.wait_reason = None
            return REASON_IDLE
        note = "; ".join(f"left out {n}: {why}" for n, why in dropped.items()) or None
        try:
            job = self.jobs.start(names, auto=True, note=note)
        except JobError as exc:  # a human started one of the steps a moment ago
            self.wait_reason = str(exc)
            return f"waiting: {exc}"
        if self._backup is not None and self._latest_backup is not None:
            self._backup_wanted = backup_due(self._latest_backup(), now, cfg["backup_hours"])
        self._note(self.pending, now, f"started {', '.join(names)}", job.id)
        log.info("automatic run %s started: %s", job.id, ", ".join(names))
        self.pending = self.wait_reason = None
        return f"started run {job.id}"

    def _note(self, slot: datetime, now: datetime, text: str, job_id: str | None = None) -> None:
        if text.startswith("skipped"):
            log.warning("automatic run for %s %s", timeutil.fmt_datetime(slot), text)
        self.outcomes.appendleft(Outcome(slot=slot, at=now, text=text, job_id=job_id))

    # -- leading -----------------------------------------------------------

    def _lead(self) -> bool:
        """Hold the leader lock, taking it if it is free. Logged only when it changes."""
        if self._held is not None:
            return True
        held = lock.acquire(self.lock_path, trust_os_lock=True)
        if held is None:
            if self.reason != REASON_FOLLOWER:
                log.info("automatic runs: another panel process leads (%s)", self.lock_path)
            return False
        self._held = held
        log.info("automatic runs: this window runs them (%s)", self.lock_path)
        return True

    def _release(self) -> None:
        if self._held is not None:
            self._held.release()
            self._held = None

    # -- what the pages show -----------------------------------------------

    def status(self, now: datetime | None = None) -> AutoStatus:
        now = now or datetime.now(UTC)
        cfg = settings_of(self._settings())
        names, dropped = plan(cfg["steps"], self.jobs.steps())
        on = cfg["enabled"] and bool(cfg["times"])
        with self._mutex:
            leader = self._held is not None
            pending, reason, outcomes = self.pending, self.reason, list(self.outcomes)
        return AutoStatus(
            enabled=cfg["enabled"],
            times=cfg["times"],
            steps=names,
            dropped=dropped,
            grace_minutes=cfg["grace_minutes"],
            backup_hours=cfg["backup_hours"] if self._backup is not None else 0.0,
            error=cfg["error"],
            next_due=next_slot(now, cfg["times"], self._tz()) if on else None,
            pending=pending,
            leader=leader,
            holder=None if leader or not on else lock.read_holder(self.lock_path),
            reason=reason,
            outcomes=outcomes,
        )

    # -- the thread --------------------------------------------------------

    def start(self) -> None:
        """Start the timer thread (the app's lifespan calls this; tests never do)."""
        self._start_thread()

    def _start_thread(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        with self._mutex:
            self._stopping = False
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="autorun", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """Stop the timer: no run starts after this returns. Releases the leader lock."""
        with self._mutex:
            self._stopping = True
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
            if not self._thread.is_alive():
                self._thread = None
        with self._mutex:
            self._release()

    def _loop(self) -> None:
        try:
            while not self._stop.is_set():
                try:
                    self.tick()
                except Exception:  # a broken settings file must not end the loop
                    log.exception("automatic runs: tick failed")
                self._stop.wait(self.poll_seconds)
        finally:
            with self._mutex:
                self._release()
