"""Automatic publishing while the control panel is open.

A background thread wakes every `poll_seconds` and, when the switch in
`publish/config.yaml` (`auto_publish_enabled`, edited from the publishing page) is on and
the last automatic run is older than `auto_publish_interval_minutes`, asks the
`JobManager` for one `run_publish.py --live` run: the very run cron would make, so the
slots, caps, gap, breaking rules and the per-draft claim all apply unchanged. A tick that
finds another run in progress (or the pipeline lock held by cron) simply tries again on
the next poll. Nothing here posts by itself: `run_publish.py` stays a dry run unless
`PUBLISH_ENABLED=1` is set, and the loop does not even start a run without it, so a
fifteen-minute rehearsal never fills the runs page.

`tick(now)` is the whole decision and takes the clock as a parameter so tests drive it
without the thread. The app starts and stops the thread in its lifespan
(`panel/app.py`), so closing the desktop window ends it with the server.
"""

from __future__ import annotations

import logging
import os
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from panel.jobs import JobError, JobManager
from publish.scheduler import load_publish_config

log = logging.getLogger(__name__)

POLL_SECONDS = 30.0

REASON_OFF = "off"
REASON_NOT_LIVE = "PUBLISH_ENABLED is not 1"
REASON_WAITING = "waiting for the interval"
REASON_STARTED = "started a publish run"


def publish_live() -> bool:
    """Whether a live run would post: run_publish.py posts only with PUBLISH_ENABLED=1."""
    return os.environ.get("PUBLISH_ENABLED") == "1"


@dataclass
class AutoStatus:
    """What the publishing and approved pages show about the loop."""

    enabled: bool
    interval_minutes: int
    live: bool
    last_started: datetime | None
    next_due: datetime | None
    reason: str

    @property
    def active(self) -> bool:
        """On and able to post."""
        return self.enabled and self.live


class AutoPublisher:
    def __init__(
        self,
        jobs: JobManager,
        settings: Callable[[], dict[str, Any]] = load_publish_config,
        live: Callable[[], bool] = publish_live,
        poll_seconds: float = POLL_SECONDS,
    ) -> None:
        self.jobs = jobs
        self._settings = settings
        self._live = live
        self.poll_seconds = poll_seconds
        self.last_started: datetime | None = None
        self.reason: str = REASON_OFF
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # -- the decision ------------------------------------------------------

    def tick(self, now: datetime | None = None) -> str:
        """Start a run if one is due; returns why it did or did not."""
        now = now or datetime.now(UTC)
        cfg = self._settings()
        if not cfg.get("auto_publish_enabled"):
            self.reason = REASON_OFF
            return self.reason
        if not self._live():
            self.reason = REASON_NOT_LIVE
            return self.reason
        due = self._next_due(now, cfg)
        if due is not None and now < due:
            self.reason = REASON_WAITING
            return self.reason
        try:
            self.jobs.start_publish_auto()
        except JobError as exc:  # a run in progress: try again on the next poll
            self.reason = f"skipped: {exc}"
            return self.reason
        self.last_started = now
        self.reason = REASON_STARTED
        log.info("automatic publishing: started a publish run")
        return self.reason

    def _next_due(self, now: datetime, cfg: dict[str, Any]) -> datetime | None:
        if self.last_started is None:
            return None
        return self.last_started + timedelta(minutes=int(cfg["auto_publish_interval_minutes"]))

    def status(self, now: datetime | None = None) -> AutoStatus:
        now = now or datetime.now(UTC)
        cfg = self._settings()
        enabled = bool(cfg.get("auto_publish_enabled"))
        return AutoStatus(
            enabled=enabled,
            interval_minutes=int(cfg["auto_publish_interval_minutes"]),
            live=self._live(),
            last_started=self.last_started,
            next_due=self._next_due(now, cfg) if enabled else None,
            reason=self.reason,
        )

    # -- the thread --------------------------------------------------------

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="autopublish", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception:  # a broken settings file must not end the loop
                log.exception("automatic publishing: tick failed")
            self._stop.wait(self.poll_seconds)
