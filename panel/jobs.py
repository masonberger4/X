"""Run orchestrator steps from the web UI, each run in a background thread of its own.

Runs go side by side: every step takes its own lock (`ops.lock.step_lock_path` with the
step's lock name, `lock:` in ops/config.yaml) while it runs, so a draft run and a verify
run, here or in another desktop window or from cron, run at once, while the same step
never runs twice. Starting a run whose step is already running in this window is refused
up front; a step another window or cron is running is skipped as `locked` when its turn
comes. "Publish now" takes the publish step's lock, so one publish at a time. A run holds
only the steps it has not finished yet: once an automatic run's ingest is done, ingest can
be started again by hand while that run drafts.

Automatic runs (`start(..., auto=True)`, started by `panel/autorun.py` at the times in
ops/config.yaml) may launch only the scripts in `ops.autorun.AUTO_SCRIPTS` (ingest, score,
draft, verify, feedback, evolve; never the publisher, run_ops.py or pipeline_cli.py) and run
with posting switched off in their environment, so even a publisher reached some other way
would only rehearse. Their run_id carries `ops.store.AUTO_RUN_MARK`, and like cron's run
they record a health report and may send an alert when they finish.

The panel never builds an argv of its own: a job names steps from `ops/config.yaml`
and `ops/runner.py` runs exactly what that file says, under the same `ops/lock.py`
lock cron takes. So a run started here is the same run cron would start, and a step
that is disabled in the config (publishing, by default) is skipped, not run.

The one run the panel builds itself carries the publisher's live flag: "Publish now"
(`start_publish_now`), the approved page's button, runs
`run_publish.py --live --now --draft ID` for the draft the human pointed at. Posting is
manual only: nothing here starts a publish run on a timer, no other argv carries the flag,
ops/config.yaml never does, and run_publish.py still posts nothing unless
PUBLISH_ENABLED=1.

Results are recorded in `pipeline_runs` through `ops/store.py`. Manual runs never send
alerts: a human is already watching.
"""

from __future__ import annotations

import logging
import sys
import threading
import uuid
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ops import autorun, runner, store
from ops.runner import Step, StepResult

log = logging.getLogger(__name__)

STATE_RUNNING = "running"
STATE_DONE = "done"
STATE_LOCKED = "locked"
STATE_ERROR = "error"
STATE_STOPPED = "stopped"

# Refused before anything is spawned, however the config got that way. ops/config.yaml
# must never carry the publisher's live flag (a step 5 test asserts it); this is the
# second lock on the same door.
FORBIDDEN_ARGS = ("--live",)

PUBLISH_NOW = "publish now"  # the step name of a start_publish_now() run
PUBLISH_CLI = "run_publish.py"
PUBLISH_LOCK = "publish"  # "Publish now" takes the publish step's lock: <lock_path>.publish
# An automatic run's environment: run_publish.py treats posting as off unless this is "1",
# and load_dotenv never overrides a variable that is already set, so .env cannot turn it on.
AUTO_ENV = {"PUBLISH_ENABLED": "0"}


class JobError(RuntimeError):
    """A job could not be started: unknown step, nothing selected, or one already running."""


@dataclass
class Job:
    id: str
    steps: list[str]
    started_at: datetime
    plan: list[Step] = field(default_factory=list)  # what actually runs, in order
    finished_at: datetime | None = None
    state: str = STATE_RUNNING
    results: list[StepResult] = field(default_factory=list)
    error: str | None = None
    stopping: str | None = None  # set by cancel(); the finished job becomes STATE_STOPPED
    active_step: str | None = None  # the step whose process is live right now
    active_since: datetime | None = None
    active_stdout: str = ""  # that step's log so far, refreshed as it writes
    active_stderr: str = ""
    publish: bool = False  # a "Publish now" run
    auto: bool = False  # started by the panel's timer (panel/autorun.py), not a human
    env: dict[str, str] = field(default_factory=dict)  # overrides for the steps' environment
    note: str | None = None  # what an automatic run left out, and why
    stop_event: threading.Event = field(default_factory=threading.Event, repr=False)

    @property
    def running(self) -> bool:
        return self.state == STATE_RUNNING

    @property
    def lock_names(self) -> set[str]:
        """The locks of the steps this run has not finished (the live step and those still
        to come), so another run of one of them is refused. A finished step is free again;
        the runner's per-step lock still keeps two copies of a step from overlapping."""
        done = {r.name for r in self.results}
        return {s.lock_name for s in self.plan if s.name not in done}

    @property
    def duration_seconds(self) -> float | None:
        if self.finished_at is None:
            return None
        return (self.finished_at - self.started_at).total_seconds()

    @property
    def failed_steps(self) -> list[str]:
        return [r.name for r in self.results if r.failed]

    @property
    def pending_steps(self) -> list[str]:
        """Steps of this job that have neither finished nor started."""
        seen = {r.name for r in self.results} | ({self.active_step} if self.active_step else set())
        return [s for s in self.steps if s not in seen]


def _now() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


class JobManager:
    """Owns the background runs: any number at once, never two of the same step. Thread-safe;
    keeps the last few jobs in memory."""

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
        self._live: list[Job] = []  # runs in flight, newest first
        self._history: list[Job] = []
        # Called with each run once its steps are over (finished, failed or stopped), before
        # it leaves the live list: panel/app.py's _after_run tidies up after a studio run.
        self.on_finish: Callable[[Job], None] | None = None

    # -- configuration -----------------------------------------------------

    def steps(self) -> list[Step]:
        return runner.steps_from_config(self.cfg)

    def step_names(self) -> list[str]:
        return [s.name for s in self.steps()]

    # -- state -------------------------------------------------------------

    def running(self) -> list[Job]:
        """Every pipeline run in flight, newest first (not "Publish now"; see `publishing()`)."""
        with self._mutex:
            return [j for j in self._live if not j.publish]

    def current(self) -> Job | None:
        """The newest pipeline run in flight, if any."""
        jobs = self.running()
        return jobs[0] if jobs else None

    def publishing(self) -> Job | None:
        """The "Publish now" run in flight, if any."""
        with self._mutex:
            return next((j for j in self._live if j.publish), None)

    def _slots(self, extra: list[Step] | None = None) -> dict[str, int]:
        """How many runs each lock name allows at once (a step's `slots`, the most any
        step sharing the lock gives)."""
        out: dict[str, int] = {}
        for s in [*self.steps(), *(extra or [])]:
            out[s.lock_name] = max(out.get(s.lock_name, 1), s.slots)
        return out

    @staticmethod
    def _full(jobs: list[Job], slots: dict[str, int]) -> set[str]:
        """Lock names every slot of which a run in `jobs` holds."""
        counts = Counter(name for j in jobs for name in j.lock_names)
        return {name for name, n in counts.items() if n >= slots.get(name, 1)}

    def busy_steps(self) -> set[str]:
        """Step names that cannot start now: every slot of their lock is taken by a run in
        this window."""
        slots = self._slots()
        with self._mutex:
            full = self._full(self._live, slots)
        return {s.name for s in self.steps() if s.lock_name in full}

    def queued_behind(self, slow: set[str]) -> dict[str, str]:
        """Busy steps that nothing is running: every run holding their lock has them still to
        come behind its live step, and that live step is one of `slow` (the skip_when_busy
        steps). {step name: the live step it waits behind}. That run gets to them when the
        slow step ends, so an automatic run time can leave them out instead of waiting for
        them (panel/autorun.py). A step whose lock a live step holds is never one of them."""
        out: dict[str, str] = {}
        slots = self._slots()
        with self._mutex:
            live = list(self._live)
            full = self._full(live, slots)
            for step in self.steps():
                if step.lock_name not in full:
                    continue  # a slot is free: it can start now
                ahead: list[str] = []
                for job in live:
                    if step.lock_name not in job.lock_names:
                        continue
                    active = next((s for s in job.plan if s.name == job.active_step), None)
                    if active is None or active.name not in slow:
                        break  # between steps, or behind a step that ends soon: busy
                    if active.lock_name == step.lock_name:
                        break  # the live step itself holds the lock
                    ahead.append(active.name)
                else:
                    if ahead:
                        out[step.name] = ahead[0]
        return out

    def history(self) -> list[Job]:
        with self._mutex:
            return list(self._live) + self._history

    def cancel(self, reason: str = "stopped by the operator", job_id: str | None = None) -> bool:
        """End a run in progress: the live step and everything it launched are killed,
        the remaining steps are skipped, and the job records why. With `job_id`, only that
        run; without it, every run in flight. False if none was running."""
        with self._mutex:
            jobs = [j for j in self._live if j.running and (job_id is None or j.id == job_id)]
            for job in jobs:
                job.stopping = reason
        for job in jobs:
            runner.terminate_active(job.stop_event)
        return bool(jobs)

    # -- starting ----------------------------------------------------------

    def start(self, step_names: list[str], *, auto: bool = False, note: str | None = None) -> Job:
        """Run the named steps from ops/config.yaml in their configured order. With `auto`
        (the panel's timer) every step must pass `ops.autorun.ineligible`, checked here so no
        caller can skip it, and the run's environment switches posting off."""
        wanted = [n.strip() for n in step_names if n and n.strip()]
        if not wanted:
            raise JobError("select at least one step to run")
        known = self.step_names()
        unknown = [n for n in wanted if n not in known]
        if unknown:
            raise JobError(f"unknown step(s): {', '.join(sorted(unknown))}")
        for step in self.steps():
            if step.name in wanted:
                if autorun.posts_live(step.argv):
                    raise JobError(
                        f"step {step.name!r} carries {FORBIDDEN_ARGS[0]}; refusing to run it"
                    )
                if auto and (why := autorun.ineligible(step)) is not None:
                    raise JobError(f"step {step.name!r} cannot run automatically: {why}")
        plan = [s for s in self.steps() if s.name in wanted]
        return self._launch(
            [s.name for s in plan],
            plan,
            auto=auto,
            env=dict(AUTO_ENV) if auto else None,
            note=note,
        )

    def start_publish_now(self, draft_id: int) -> Job:
        """Post one approved draft now: `run_publish.py --live --now --draft ID`, as its own
        run, so its log lands on the runs page like any other step. The caps in
        publish/config.yaml still apply and run_publish.py stays a dry run unless
        PUBLISH_ENABLED=1 is set."""
        draft_id = int(draft_id)
        if draft_id <= 0:
            raise JobError("publish now needs a draft id")
        return self._launch_publish(PUBLISH_NOW, ["--now", "--draft", str(draft_id)])

    def _launch_publish(self, name: str, extra: list[str]) -> Job:
        """The publisher with the live flag, as a run of its own. The step's timeout comes
        from the config's publish step; this is the only place the panel passes the flag,
        and only for a draft a human pressed "Publish now" on."""
        base = next((s for s in self.steps() if s.name == "publish"), None)
        timeout = base.timeout_seconds if base is not None else 300
        step = Step(
            name=name,
            argv=["python", PUBLISH_CLI, "--live", *extra],
            enabled=True,
            required=False,
            timeout_seconds=timeout,
            lock=PUBLISH_LOCK,
        )
        return self._launch([name], [step], publish=True)

    def _launch(
        self,
        names: list[str],
        plan: list[Step],
        publish: bool = False,
        *,
        auto: bool = False,
        env: dict[str, str] | None = None,
        note: str | None = None,
    ) -> Job:
        if auto and publish:  # an automatic run never posts; nothing may combine the two
            raise JobError("an automatic run cannot publish")
        slots = self._slots(plan)
        with self._mutex:
            full = self._full([j for j in self._live if j.running], slots)
            wanted = {s.lock_name for s in plan} & full
            clash = [j for j in self._live if j.running and j.lock_names & wanted]
            if clash:
                if publish:
                    raise JobError("a publish is already in progress")
                asked = [s.name for s in plan if s.lock_name in wanted]
                # Name the steps that actually hold the lock: ingest and score share one,
                # so a score refused during an ingest run must say ingest, not score.
                holders: list[str] = []
                for j in clash:
                    done = {r.name for r in j.results}
                    for s in j.plan:
                        if s.name not in done and s.lock_name in wanted and s.name not in holders:
                            holders.append(s.name)
                if holders == asked:
                    msg = f"{', '.join(asked)} is already running; wait for it to finish"
                else:
                    msg = (
                        f"{', '.join(asked)} cannot start while {', '.join(holders)} is running "
                        "or queued (they share a lock); wait for it to finish"
                    )
                raise JobError(msg)
            job = Job(
                id=uuid.uuid4().hex[:8],
                steps=names,
                started_at=_now(),
                plan=plan,
                publish=publish,
                auto=auto,
                env=dict(env or {}),
                note=note,
            )
            self._live.insert(0, job)
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
            if self.on_finish is not None:
                try:
                    self.on_finish(job)
                except Exception:  # nor may the hook
                    log.exception("panel run %s: after-run hook failed", job.id)
            job.finished_at = _now()
            if job.stopping and job.state == STATE_RUNNING:
                job.state = STATE_STOPPED
                job.error = job.stopping
            elif job.state == STATE_RUNNING:
                job.state = STATE_DONE
            with self._mutex:
                self._history.insert(0, job)
                del self._history[self._history_size :]
                self._live = [j for j in self._live if j is not job]

    def _run_under_lock(self, job: Job) -> None:
        """Run the job's steps; each takes its own lock as it starts (`run_steps(lock_path=)`),
        and one that another window or cron is running is skipped as locked."""
        if job.stopping:  # stopped before its thread got here: run nothing
            return
        tail = int(self.cfg.get("run_log_tail_chars", 4000))

        # Results land on the job as each step finishes, so the runs page can show
        # progress mid-run instead of one block when the whole list returns.
        def started(step: Step) -> None:
            job.active_stdout, job.active_stderr = "", ""
            job.active_step, job.active_since = step.name, _now()

        def output(step: Step, stdout: str, stderr: str) -> None:
            job.active_stdout, job.active_stderr = stdout, stderr

        def finished(result: StepResult) -> None:
            job.active_step, job.active_since = None, None
            job.active_stdout, job.active_stderr = "", ""
            job.results.append(result)

        runner.run_steps(
            job.plan,
            cwd=self.repo_root,
            python=self.python,
            tail_chars=tail,
            on_start=started,
            on_result=finished,
            on_output=output,
            stop=job.stop_event,
            lock_path=self.cfg["lock_path"],
            env=job.env or None,
        )
        locked = [r.name for r in job.results if r.skipped_reason == runner.SKIP_LOCKED]
        if locked and len(locked) == len(job.results):
            log.info("panel run %s: every step is running elsewhere", job.id)
            job.state = STATE_LOCKED
            job.error = (
                "another publish holds the publish lock; try again when it finishes"
                if job.publish
                else f"{', '.join(locked)} is already running in another window or a cron "
                "run; try again when it finishes"
            )
            return
        self._record(job)

    def _record(self, job: Job) -> None:
        mark = store.AUTO_RUN_MARK if job.auto else "-panel-"
        run_id = f"{job.started_at.strftime('%Y%m%dT%H%M%SZ')}{mark}{job.id}"
        conn = store.connect(self._db_path)
        try:
            store.record_results(conn, run_id, job.results)
            if job.auto:
                self._health_and_alert(conn)
        finally:
            conn.close()

    def _health_and_alert(self, conn: Any) -> None:
        """After an automatic run, what cron's run does: record a health report and send an
        alert when a check is bad (cooldowns in ops/config.yaml apply). Nobody is watching an
        automatic run, so this is how a failure reaches the operator. Never raises."""
        import run_ops  # the CLI module owns the report-and-alert path; imported lazily

        try:
            db_path = Path(self._db_path) if self._db_path else store.db_path()
            run_ops.health_and_alert(conn, self.cfg, db_path, _now(), send=True)
        except Exception:
            log.exception("automatic run: health report or alert failed")
