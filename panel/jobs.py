"""Run orchestrator steps from the web UI, in a background thread, one at a time.

The panel never builds an argv of its own: a job names steps from `ops/config.yaml`
and `ops/runner.py` runs exactly what that file says, under the same `ops/lock.py`
lock cron takes. So a run started here is the same run cron would start, and a step
that is disabled in the config (publishing, by default) is skipped, not run.

Results are recorded in `pipeline_runs` through `ops/store.py` and a health report is
recomputed afterwards. Manual runs never send alerts: a human is already watching.
"""

from __future__ import annotations

import logging
import sys
import threading
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ops import lock, runner, store
from ops.runner import Step, StepResult

log = logging.getLogger(__name__)

STATE_RUNNING = "running"
STATE_DONE = "done"
STATE_LOCKED = "locked"
STATE_ERROR = "error"

# Refused before anything is spawned, however the config got that way. ops/config.yaml
# must never carry the publisher's live flag (a step 5 test asserts it); this is the
# second lock on the same door.
FORBIDDEN_ARGS = ("--live",)


class JobError(RuntimeError):
    """A job could not be started: unknown step, nothing selected, or one already running."""


@dataclass
class Job:
    id: str
    steps: list[str]
    started_at: datetime
    finished_at: datetime | None = None
    state: str = STATE_RUNNING
    results: list[StepResult] = field(default_factory=list)
    error: str | None = None

    @property
    def running(self) -> bool:
        return self.state == STATE_RUNNING

    @property
    def duration_seconds(self) -> float | None:
        if self.finished_at is None:
            return None
        return (self.finished_at - self.started_at).total_seconds()

    @property
    def failed_steps(self) -> list[str]:
        return [r.name for r in self.results if r.failed]


def _now() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


class JobManager:
    """Owns the single background slot. Thread-safe; keeps the last few jobs in memory."""

    def __init__(
        self,
        cfg: dict[str, Any],
        repo_root: Path,
        db_path: Path | None = None,
        history_size: int = 10,
        python: str = sys.executable,
    ) -> None:
        self.cfg = cfg
        self.repo_root = Path(repo_root)
        self.python = python
        self._db_path = db_path
        self._history_size = history_size
        self._mutex = threading.Lock()
        self._current: Job | None = None
        self._history: list[Job] = []

    # -- configuration -----------------------------------------------------

    def steps(self) -> list[Step]:
        return runner.steps_from_config(self.cfg)

    def step_names(self) -> list[str]:
        return [s.name for s in self.steps()]

    # -- state -------------------------------------------------------------

    def current(self) -> Job | None:
        with self._mutex:
            return self._current

    def history(self) -> list[Job]:
        with self._mutex:
            jobs = ([self._current] if self._current else []) + self._history
        return jobs

    # -- starting ----------------------------------------------------------

    def start(self, step_names: list[str]) -> Job:
        wanted = [n.strip() for n in step_names if n and n.strip()]
        if not wanted:
            raise JobError("select at least one step to run")
        known = self.step_names()
        unknown = [n for n in wanted if n not in known]
        if unknown:
            raise JobError(f"unknown step(s): {', '.join(sorted(unknown))}")
        for step in self.steps():
            if step.name in wanted:
                bad = [a for a in step.argv if a in FORBIDDEN_ARGS]
                if bad:
                    raise JobError(f"step {step.name!r} carries {bad[0]}; refusing to run it")
        ordered = [n for n in known if n in wanted]

        with self._mutex:
            if self._current is not None and self._current.running:
                raise JobError("a run is already in progress")
            job = Job(id=uuid.uuid4().hex[:8], steps=ordered, started_at=_now())
            self._current = job
        thread = threading.Thread(target=self._execute, args=(job,), daemon=True)
        thread.start()
        return job

    # -- execution ---------------------------------------------------------

    def _execute(self, job: Job) -> None:
        try:
            self._run_under_lock(job)
        except Exception as exc:  # a crash here must not take the web app down
            log.exception("panel run %s failed", job.id)
            job.state = STATE_ERROR
            job.error = f"{type(exc).__name__}: {exc}"
        finally:
            job.finished_at = _now()
            if job.state == STATE_RUNNING:
                job.state = STATE_DONE
            with self._mutex:
                self._history.insert(0, job)
                del self._history[self._history_size :]
                self._current = None

    def _run_under_lock(self, job: Job) -> None:
        held = lock.acquire(self.cfg["lock_path"])
        if held is None:
            log.info("panel run %s: the pipeline lock is held (a cron run is in flight)", job.id)
            job.state = STATE_LOCKED
            job.error = "another run holds the pipeline lock; try again when it finishes"
            return
        with held:
            tail = int(self.cfg.get("run_log_tail_chars", 4000))
            job.results = runner.run_steps(
                self.steps(),
                only=job.steps,
                cwd=self.repo_root,
                python=self.python,
                tail_chars=tail,
            )
            self._record(job)

    def _record(self, job: Job) -> None:
        run_id = f"{job.started_at.strftime('%Y%m%dT%H%M%SZ')}-panel-{job.id}"
        conn = store.connect(self._db_path)
        try:
            store.record_results(conn, run_id, job.results)
        finally:
            conn.close()
