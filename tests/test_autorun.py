"""Automatic runs: everything but publishing, at a few times a day while the panel is open.

ops/autorun.py (pure: times, the slot clock, which steps may run), ops/config.py's
save_auto_run (the panel's one write to ops/config.yaml), panel/jobs.py's automatic mode
(allowlist, posting switched off in the environment) and panel/autorun.py's AutoRunner
(the decision in `tick(now)`, the leader lock, the thread).
"""

from __future__ import annotations

import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
import yaml

import run_ops
import run_publish
from ops import autorun, store
from ops.config import load_ops_config, save_auto_run
from ops.health import Thresholds, check_sources
from ops.models import SourceRun
from ops.runner import Step, StepResult, steps_from_config
from panel import autorun as panel_autorun
from panel.autorun import AutoRunner
from panel.jobs import Job, JobError, JobManager

LA = ZoneInfo("America/Los_Angeles")
REPO = Path(__file__).resolve().parents[1]


def la(y, m, d, hh, mm=0):
    return datetime(y, m, d, hh, mm, tzinfo=LA).astimezone(UTC)


# --------------------------------------------------------------------------- times


def test_parse_times_normalises_sorts_and_dedupes():
    assert autorun.parse_times("18:00, 6:00 12:00,06:00") == ["06:00", "12:00", "18:00"]
    assert autorun.parse_times(["06:00", 720]) == ["06:00", "12:00"]  # YAML's 12:00 -> 720
    assert autorun.parse_times("") == [] and autorun.parse_times(None) == []


@pytest.mark.parametrize("bad", ["24:00", "6", "06:60", "noon", "06:00\nsteps: [publish]"])
def test_parse_times_refuses_what_is_not_a_time(bad):
    with pytest.raises(ValueError):
        autorun.parse_times(bad)


def test_parse_times_bounds_how_many_and_how_close():
    with pytest.raises(ValueError, match="at most"):
        autorun.parse_times([f"{h:02d}:00" for h in range(0, 24, 2)])  # 12 times
    with pytest.raises(ValueError, match="apart"):
        autorun.parse_times("06:00, 06:30")
    with pytest.raises(ValueError, match="apart"):
        autorun.parse_times("23:30, 00:10")  # 40 minutes across midnight
    assert autorun.parse_times("00:00, 03:00, 06:00, 09:00, 12:00, 15:00, 18:00, 21:00")


def test_slots_between_fires_each_time_once_and_never_backwards():
    times = ["06:00", "12:00", "18:00"]
    assert autorun.slots_between(la(2026, 10, 1, 5), la(2026, 10, 1, 6), times, LA) == [
        la(2026, 10, 1, 6)
    ]
    # the boundary belongs to the earlier tick only
    assert autorun.slots_between(la(2026, 10, 1, 6), la(2026, 10, 1, 7), times, LA) == []
    # two days asleep: every time in between, oldest first
    got = autorun.slots_between(la(2026, 10, 1, 19), la(2026, 10, 3, 7), times, LA)
    assert got[0] == la(2026, 10, 2, 6) and got[-1] == la(2026, 10, 3, 6) and len(got) == 4
    # the clock went back: nothing
    assert autorun.slots_between(la(2026, 10, 1, 7), la(2026, 10, 1, 5), times, LA) == []


def test_slots_follow_the_wall_clock_across_dst():
    # fall back (2026-11-01): 01:30 happens twice on the wall clock but runs once
    got = autorun.slots_between(la(2026, 11, 1, 0), la(2026, 11, 1, 4), ["01:30"], LA)
    assert len(got) == 1 and got[0].astimezone(UTC).hour == 8  # the first 01:30, PDT
    # spring forward (2026-03-08): 02:30 does not exist; it still runs once that night
    got = autorun.slots_between(la(2026, 3, 8, 0), la(2026, 3, 8, 6), ["02:30"], LA)
    assert len(got) == 1
    # 06:00 is 06:00 local on both sides of the change
    assert autorun.next_slot(la(2026, 11, 1, 3), ["06:00"], LA) == la(2026, 11, 1, 6)
    assert autorun.next_slot(la(2026, 11, 2, 7), ["06:00"], LA) == la(2026, 11, 3, 6)


# --------------------------------------------------------------------------- never publish


@pytest.mark.parametrize(
    "argv",
    [
        ["python", "run_publish.py"],
        ["python", "run_publish.py", "--live"],
        ["python", "run_ingest.py", "--liv"],
        ["python", "-m", "run_publish", "--now"],
        ["python", "pipeline_cli.py", "run_publish.py"],
        ["pipeline-cli", "run_publish.py"],
        ["python", "-c", "import run_publish; run_publish.main(['--live'])"],
        ["python", "run_ops.py", "run"],
        ["python", "run_app.py"],
        ["sh", "-c", "python run_ingest.py"],
    ],
)
def test_a_step_that_could_post_never_runs_automatically(argv):
    for name in ("publish", "Publish", "post", "refresh", "ingest"):
        assert autorun.ineligible(Step(name, argv)) is not None


def test_the_pipeline_steps_may_run_automatically():
    for script in sorted(autorun.AUTO_SCRIPTS):
        assert autorun.ineligible(Step("x", ["python", script])) is None
    assert "run_publish.py" not in autorun.AUTO_SCRIPTS


def test_the_studio_runs_automatically_but_its_buttons_never_do():
    assert "run_studio.py" in autorun.AUTO_SCRIPTS
    assert autorun.ineligible(Step("studio", ["python", "run_studio.py"])) is None
    for flag in ("--now", "--resume-only"):
        button = Step("studio_x", ["python", "run_studio.py", flag], manual=True)
        assert "manual" in autorun.ineligible(button)
    refused = autorun.ineligible(Step("x", ["python", "run_ops.py", "run"]))
    assert refused == "only ingest, score, movers, feedback and studio run automatically"


SHIPPED_AUTO_STEPS = [
    "ingest",
    "score",
    "movers",
    "studio_scan",
    "studio",
    "feedback",
    "studio_learn",
]


def test_the_shipped_config_runs_everything_but_publishing():
    cfg = load_ops_config()
    s = autorun.settings_of(cfg)
    steps = steps_from_config(cfg)
    names, dropped = autorun.plan(s["steps"], steps)
    assert s["error"] is None and s["times"] == [
        "01:00",
        "03:00",
        "06:00",
        "09:32",
        "12:00",
        "15:00",
    ]
    assert names == SHIPPED_AUTO_STEPS and not dropped
    assert "publish" not in s["steps"]
    assert "--live" not in (REPO / "ops" / "config.yaml").read_text(encoding="utf-8")
    # the studio is the one step that sits a run time out while a session is still going
    assert [st.name for st in steps if st.skip_when_busy] == ["studio"]
    # the studio page's buttons are manual: even listed by mistake they never run on a timer
    buttons = ["studio_now", "studio_resume", "studio_scan_now", "studio_learn_now"]
    names, dropped = autorun.plan(buttons, steps)
    assert names == [] and set(dropped) == set(buttons)
    assert all("manual" in why for why in dropped.values())


def test_the_publisher_no_longer_takes_an_abbreviated_live_flag(capsys):
    with pytest.raises(SystemExit) as exc:
        run_publish.main(["--liv", "--now", "--draft", "1"])
    assert exc.value.code == 2


def test_run_ops_refuses_an_abbreviated_live_flag(tmp_path):
    cfg = {"steps": [{"name": "p", "argv": ["python", "run_publish.py", "--liv"]}]}
    path = tmp_path / "ops.yaml"
    path.write_text(yaml.safe_dump({**cfg, "lock_path": str(tmp_path / "l")}), encoding="utf-8")
    assert run_ops.main(["--config", str(path), "run"]) == 1


def test_load_ops_config_fills_the_automatic_defaults(tmp_path):
    path = tmp_path / "ops.yaml"
    path.write_text("steps: []\n", encoding="utf-8")
    cfg = load_ops_config(path)
    assert cfg["auto_run_enabled"] is False and cfg["auto_run_times"] == []


# --------------------------------------------------------------------------- the settings write


SAMPLE = """# top comment
steps:
  - name: ingest
    argv: ["python", "run_ingest.py"]
    enabled: true   # a step's own switch
lock_path: ./pipeline.lock

# the switch
auto_run_enabled: false   # flipped from the runs page
auto_run_times:
  - "06:00"
  - "18:00"
auto_run_steps: [ingest]
health:
  max_hours_since_ingest: 13
"""


def test_save_auto_run_edits_two_keys_and_keeps_everything_else(tmp_path):
    path = tmp_path / "ops.yaml"
    path.write_text(SAMPLE, encoding="utf-8")
    before = yaml.safe_load(SAMPLE)
    assert save_auto_run(True, "12:00, 6:00", path) == {
        "auto_run_enabled": True,
        "auto_run_times": ["06:00", "12:00"],
    }
    text = path.read_text(encoding="utf-8")
    after = yaml.safe_load(text)
    assert after["auto_run_enabled"] is True and after["auto_run_times"] == ["06:00", "12:00"]
    for key in ("steps", "lock_path", "auto_run_steps", "health"):
        assert after[key] == before[key]
    assert "# a step's own switch" in text and "# flipped from the runs page" in text
    assert "    enabled: true   # a step's own switch\n" in text  # byte for byte
    assert 'auto_run_times: ["06:00", "12:00"]' in text  # quoted, never 720


def test_save_auto_run_appends_missing_keys_and_refuses_bad_times(tmp_path):
    path = tmp_path / "ops.yaml"
    path.write_text("steps: []\nlock_path: ./x\n", encoding="utf-8")
    save_auto_run(False, ["07:00"], path)
    assert yaml.safe_load(path.read_text(encoding="utf-8"))["auto_run_times"] == ["07:00"]
    original = path.read_text(encoding="utf-8")
    for bad in ("07:00\nsteps: [publish]", "25:00", "07:00, 07:10"):
        with pytest.raises(ValueError):
            save_auto_run(True, bad, path)
    assert path.read_text(encoding="utf-8") == original


# --------------------------------------------------------------------------- JobManager, automatic


def _fake_scripts(root: Path) -> None:
    probe = (
        "import os, pathlib; pathlib.Path('env.txt').write_text("
        "os.environ.get('PUBLISH_ENABLED', 'unset'))"
    )
    for script in autorun.AUTO_SCRIPTS:
        (root / script).write_text(probe if script == "run_ingest.py" else "", encoding="utf-8")
    (root / "run_publish.py").write_text("raise SystemExit('posted')", encoding="utf-8")


def _wait(job, timeout=20.0):
    deadline = time.monotonic() + timeout
    while job.running and time.monotonic() < deadline:
        time.sleep(0.02)
    assert not job.running
    return job


def _manager(tmp_path, steps):
    cfg = {"steps": steps, "lock_path": str(tmp_path / "p.lock"), "run_log_tail_chars": 4000}
    return JobManager(cfg, tmp_path, db_path=tmp_path / "t.db", python=sys.executable)


def test_an_automatic_run_cannot_turn_posting_on(tmp_path, monkeypatch):
    _fake_scripts(tmp_path)
    monkeypatch.setenv("PUBLISH_ENABLED", "1")  # as a .env with posting on would set it
    jobs = _manager(tmp_path, [{"name": "ingest", "argv": ["python", "run_ingest.py"]}])
    job = _wait(jobs.start(["ingest"], auto=True))
    assert job.results[0].ok, job.results[0].stderr_tail
    assert (tmp_path / "env.txt").read_text(encoding="utf-8") == "0"
    conn = store.connect(tmp_path / "t.db")
    run = store.last_auto_run(conn)
    assert run is not None and store.AUTO_RUN_MARK in run["run_id"] and run["failed"] == []
    # a human's run of the same step keeps the operator's environment
    _wait(jobs.start(["ingest"]))
    assert (tmp_path / "env.txt").read_text(encoding="utf-8") == "1"


def test_an_automatic_run_refuses_anything_but_the_pipeline_scripts(tmp_path):
    _fake_scripts(tmp_path)
    jobs = _manager(
        tmp_path,
        [
            {"name": "ingest", "argv": ["python", "run_ingest.py"]},
            {"name": "refresh", "argv": ["python", "run_publish.py"]},
        ],
    )
    with pytest.raises(JobError, match="cannot run automatically"):
        jobs.start(["ingest", "refresh"], auto=True)
    assert jobs.running() == []
    with pytest.raises(JobError, match="cannot publish"):
        jobs._launch(["x"], [], publish=True, auto=True)


def test_a_finished_step_is_free_again_while_its_run_goes_on(tmp_path):
    jobs = _manager(
        tmp_path,
        [
            {"name": "fast", "argv": ["python", "-c", "print(1)"]},
            {"name": "slow", "argv": ["python", "-c", "import time; time.sleep(30)"]},
        ],
    )
    job = jobs.start(["fast", "slow"])
    deadline = time.monotonic() + 15
    while not job.results and time.monotonic() < deadline:
        time.sleep(0.02)
    assert jobs.busy_steps() == {"slow"}
    _wait(jobs.start(["fast"]))  # accepted: only the unfinished step is busy
    jobs.cancel("test over")
    _wait(job)


def test_a_run_stopped_before_its_thread_starts_runs_nothing(tmp_path):
    from panel.jobs import Job

    jobs = _manager(tmp_path, [{"name": "a", "argv": ["python", "-c", "print(1)"]}])
    job = Job(id="x", steps=["a"], started_at=datetime.now(UTC), plan=jobs.steps())
    job.stopping = "stopped at once"  # Stop won the race against the thread
    jobs._run_under_lock(job)
    assert job.results == []


# --------------------------------------------------------------------------- AutoRunner.tick


class FakeJobs:
    """A JobManager stand-in: the shipped steps, a busy set, and a record of starts."""

    def __init__(self, steps=None):
        self._steps = steps or steps_from_config(load_ops_config())
        self.busy: set[str] = set()
        # busy steps an earlier run has still to come behind a slow live step: {step: it}
        self.behind: dict[str, str] = {}
        self.started: list[tuple[list[str], bool]] = []
        self.notes: list[str | None] = []  # each started run's note (the steps left out)
        self.refuse: str | None = None

    def steps(self):
        return self._steps

    def busy_steps(self):
        return set(self.busy)

    def queued_behind(self, slow):
        return {name: ahead for name, ahead in self.behind.items() if ahead in slow}

    def start(self, names, *, auto=False, note=None):
        if self.refuse:
            raise JobError(self.refuse)
        assert auto is True
        self.started.append((list(names), auto))
        self.notes.append(note)

        class J:
            id = f"job{len(self.started)}"

        return J()

    def start_publish_now(self, *a, **k):  # pragma: no cover - must never be called
        raise AssertionError("an automatic run asked to publish")


def _settings(**over):
    cfg = {
        "auto_run_enabled": True,
        "auto_run_times": ["06:00", "12:00", "18:00"],
        "auto_run_steps": ["ingest", "score", "feedback"],
        "auto_run_grace_minutes": 60,
    }
    cfg.update(over)
    return cfg


def _runner(tmp_path, jobs=None, settings=None, name="a"):
    holder = {"cfg": settings or _settings()}
    r = AutoRunner(
        jobs or FakeJobs(),
        tmp_path / "p.lock.autorun",
        settings=lambda: holder["cfg"],
        tz=lambda: LA,
    )
    r.holder_cfg = holder
    return r


def test_a_time_fires_once_and_nothing_before_the_app_opened_is_made_up(tmp_path):
    r = _runner(tmp_path)
    r.tick(la(2026, 10, 1, 7))  # opened at 07:00: the 06:00 run is not made up
    assert r.jobs.started == []
    r.tick(la(2026, 10, 1, 11, 59))
    r.tick(
        la(
            2026,
            10,
            1,
            12,
            0,
        )
    )
    r.tick(la(2026, 10, 1, 12, 1))
    assert [s for s, _ in r.jobs.started] == [["ingest", "score", "feedback"]]
    assert r.outcomes[0].text.startswith("started") and r.outcomes[0].slot == la(2026, 10, 1, 12)


def test_switching_on_or_adding_a_past_time_never_fires_at_once(tmp_path):
    r = _runner(tmp_path, settings=_settings(auto_run_enabled=False))
    r.tick(la(2026, 10, 1, 5))
    r.tick(la(2026, 10, 1, 15))  # 06:00 and 12:00 passed while off
    r.holder_cfg["cfg"] = _settings()
    r.tick(
        la(
            2026,
            10,
            1,
            15,
            0,
        )
    )
    r.tick(la(2026, 10, 1, 15, 1))
    r.holder_cfg["cfg"] = _settings(auto_run_times=["09:00", "18:00"])  # 09:00 is past
    r.tick(la(2026, 10, 1, 15, 2))
    assert r.jobs.started == []
    r.tick(la(2026, 10, 1, 18, 0))
    assert len(r.jobs.started) == 1


def test_the_clock_going_back_does_not_fire_a_time_twice(tmp_path):
    r = _runner(tmp_path)
    r.tick(la(2026, 10, 1, 11, 59))
    r.tick(la(2026, 10, 1, 12, 1))
    r.tick(la(2026, 10, 1, 11, 0))  # clock corrected backwards
    r.tick(la(2026, 10, 1, 12, 2))
    assert len(r.jobs.started) == 1


def test_a_time_missed_while_asleep_runs_on_wake_only_within_the_grace(tmp_path):
    r = _runner(tmp_path)
    r.tick(la(2026, 10, 1, 11))
    r.tick(la(2026, 10, 1, 12, 40))  # woke 40 minutes late: runs
    assert len(r.jobs.started) == 1
    r.tick(la(2026, 10, 1, 19, 30))  # slept through 18:00, 90 minutes late: skipped
    assert len(r.jobs.started) == 1
    assert "asleep" in r.outcomes[0].text and r.outcomes[0].slot == la(2026, 10, 1, 18)
    r.tick(la(2026, 10, 3, 6, 10))  # two days asleep: one run, the latest time
    assert len(r.jobs.started) == 2


def test_a_time_waits_for_a_busy_step_then_gives_up_after_the_grace(tmp_path):
    jobs = FakeJobs()
    jobs.busy = {"score"}  # the 06:00 run is still scoring
    r = _runner(tmp_path, jobs)
    r.tick(la(2026, 10, 1, 11, 59))
    assert r.tick(la(2026, 10, 1, 12, 0)).startswith("waiting: score still running")
    jobs.busy = set()
    r.tick(la(2026, 10, 1, 12, 20))  # freed within the grace: runs
    assert len(jobs.started) == 1
    jobs.busy = {"feedback"}
    r.tick(la(2026, 10, 1, 18, 0))
    r.tick(la(2026, 10, 1, 19, 5))  # still busy past the grace: skipped, with the reason
    assert len(jobs.started) == 1
    assert r.outcomes[0].text == "skipped: feedback still running"
    jobs.refuse = "score is already running; wait for it to finish"  # a human beat us to it
    jobs.busy = set()
    r.tick(la(2026, 10, 2, 5, 59))
    assert r.tick(la(2026, 10, 2, 6, 0)).startswith("waiting: score is already running")


# The three studio steps share one lock, so a session in flight makes all three busy.
STUDIO_BUSY = {"studio", "studio_now", "studio_resume"}
REST = ["ingest", "score", "movers", "studio_scan", "feedback", "studio_learn"]


def test_a_busy_studio_sits_the_run_time_out_while_the_other_steps_start(tmp_path):
    jobs = FakeJobs()  # the shipped steps: studio is skip_when_busy
    jobs.busy = set(STUDIO_BUSY)  # the 06:00 piece is still being written at 12:00
    r = _runner(tmp_path, jobs, _settings(auto_run_steps=SHIPPED_AUTO_STEPS))
    r.tick(la(2026, 10, 1, 11, 59))
    assert r.tick(la(2026, 10, 1, 12, 0)) == "started run job1"
    assert jobs.started == [(REST, True)]
    assert jobs.notes == ["left out studio: still running from an earlier run"]
    assert r.outcomes[0].text == "started " + ", ".join(REST) and r.pending is None
    # free again by the next run time: it runs in its configured place, nothing left out
    jobs.busy = set()
    r.tick(la(2026, 10, 1, 18, 0))
    assert jobs.started[-1] == (SHIPPED_AUTO_STEPS, True) and jobs.notes[-1] is None


def test_a_studio_left_out_is_noted_beside_the_steps_that_never_run(tmp_path):
    jobs = FakeJobs()
    jobs.busy = set(STUDIO_BUSY)
    r = _runner(tmp_path, jobs, _settings(auto_run_steps=["ingest", "studio", "publish"]))
    r.tick(la(2026, 10, 1, 5, 59))
    r.tick(la(2026, 10, 1, 6, 0))
    assert jobs.started == [(["ingest"], True)]
    (note,) = jobs.notes
    assert note.startswith("left out publish: only ingest, score")
    assert note.endswith("; left out studio: still running from an earlier run")


def test_a_run_time_whose_every_step_is_still_running_is_skipped(tmp_path):
    jobs = FakeJobs()
    jobs.busy = set(STUDIO_BUSY)
    r = _runner(tmp_path, jobs, _settings(auto_run_steps=["studio"]))
    r.tick(la(2026, 10, 1, 5, 59))
    assert r.tick(la(2026, 10, 1, 6, 0)) == panel_autorun.REASON_IDLE
    assert jobs.started == [] and r.pending is None and r.wait_reason is None
    assert r.outcomes[0].text == "skipped: every step is still running"
    assert r.outcomes[0].slot == la(2026, 10, 1, 6)
    # skipped, not waiting: the session finishing later does not make 06:00 up
    jobs.busy = set()
    r.tick(la(2026, 10, 1, 6, 30))
    assert jobs.started == [] and len(r.outcomes) == 1
    r.tick(la(2026, 10, 1, 12, 0))
    assert jobs.started == [(["studio"], True)]


def test_a_busy_step_that_cannot_sit_out_still_holds_the_run_time(tmp_path):
    jobs = FakeJobs()
    jobs.busy = STUDIO_BUSY | {"feedback"}
    r = _runner(tmp_path, jobs, _settings(auto_run_steps=SHIPPED_AUTO_STEPS))
    r.tick(la(2026, 10, 1, 5, 59))
    assert r.tick(la(2026, 10, 1, 6, 0)) == "waiting: feedback still running"  # not the studio
    assert jobs.started == [] and r.pending == la(2026, 10, 1, 6)
    jobs.busy = set(STUDIO_BUSY)  # the feedback run finished; the studio session goes on
    r.tick(la(2026, 10, 1, 6, 10))
    assert jobs.started == [(REST, True)]
    assert jobs.notes == ["left out studio: still running from an earlier run"]


def test_only_a_step_marked_skip_when_busy_sits_a_run_time_out(tmp_path):
    steps = [
        Step("ingest", ["python", "run_ingest.py"]),
        Step("studio", ["python", "run_studio.py"]),  # the name alone does not make it skip
        Step("feedback", ["python", "run_feedback.py", "snapshot"], skip_when_busy=True),
    ]
    jobs = FakeJobs(steps)
    jobs.busy = {"studio", "feedback"}
    r = _runner(tmp_path, jobs, _settings(auto_run_steps=["ingest", "studio", "feedback"]))
    r.tick(la(2026, 10, 1, 5, 59))
    assert r.tick(la(2026, 10, 1, 6, 0)) == "waiting: studio still running"
    jobs.busy = {"feedback"}
    r.tick(la(2026, 10, 1, 6, 5))
    assert jobs.started == [(["ingest", "studio"], True)]
    assert jobs.notes == ["left out feedback: still running from an earlier run"]


def test_steps_waiting_behind_a_busy_studio_in_an_earlier_run_sit_the_time_out_too(tmp_path):
    jobs = FakeJobs()
    # the 06:00 run's, after its studio
    later = ["feedback", "studio_learn"]
    jobs.busy = STUDIO_BUSY | set(later)
    jobs.behind = dict.fromkeys(later, "studio")
    r = _runner(tmp_path, jobs, _settings(auto_run_steps=SHIPPED_AUTO_STEPS))
    r.tick(la(2026, 10, 1, 11, 59))
    assert r.tick(la(2026, 10, 1, 12, 0)) == "started run job1"
    assert jobs.started == [(["ingest", "score", "movers", "studio_scan"], True)]
    (note,) = jobs.notes
    assert "left out studio: still running from an earlier run" in note
    for name in later:
        assert f"left out {name}: still to run in an earlier run, after studio" in note


def _live_auto_run(jobs: JobManager, active: str, done: list[str]):
    """An automatic run of the shipped steps in `jobs`, with `done` finished and `active`
    live (no process: only the bookkeeping the manager reads)."""
    by_name = {s.name: s for s in jobs.steps()}
    job = Job(
        id="j0600",
        steps=list(SHIPPED_AUTO_STEPS),
        started_at=la(2026, 10, 1, 6),
        plan=[by_name[n] for n in SHIPPED_AUTO_STEPS],
        auto=True,
    )
    for name in done:
        job.results.append(
            StepResult(name, [], la(2026, 10, 1, 6), la(2026, 10, 1, 6, 5), exit_code=0)
        )
    job.active_step = active
    jobs._live.append(job)
    return job


def _fill_studio_slots(jobs: JobManager) -> None:
    """Live studio_now runs in every writing slot the shipped config gives but one."""
    step = next(s for s in jobs.steps() if s.name == "studio_now")
    for k in range(step.slots - 1):
        job = Job(id=f"jnow{k}", steps=["studio_now"], started_at=la(2026, 10, 1, 7), plan=[step])
        job.active_step = "studio_now"
        jobs._live.append(job)


def test_a_studio_session_outlasting_the_gap_holds_back_only_itself(tmp_path, monkeypatch):
    """The shipped run lists the studio before feedback. When its session is still going
    at the next run time, feedback and studio_learn are not running: they wait in
    that run behind the studio. The time runs ingest and score instead of waiting the grace
    out on steps that cannot start, then skipping everything."""
    cfg = load_ops_config()
    cfg["lock_path"] = str(tmp_path / "p.lock")
    jobs = JobManager(cfg, tmp_path, db_path=tmp_path / "t.db")
    _live_auto_run(jobs, "studio", done=["ingest", "score"])
    # One studio run leaves the other writing slots free; fill them (two editor's pieces).
    assert not STUDIO_BUSY & jobs.busy_steps()
    _fill_studio_slots(jobs)
    later = {"feedback", "studio_learn"}
    assert later | STUDIO_BUSY <= jobs.busy_steps()
    behind = jobs.queued_behind({"studio"})
    assert later <= set(behind) and set(behind.values()) == {"studio"}
    assert not STUDIO_BUSY & set(behind)  # the session's own lock is the live step's
    started = []

    def start(names, *, auto=False, note=None):
        started.append((list(names), auto, note))
        return type("J", (), {"id": "j1200"})()

    monkeypatch.setattr(jobs, "start", start)
    r = AutoRunner(jobs, tmp_path / "p.lock.autorun", settings=lambda: cfg, tz=lambda: LA)
    r.tick(la(2026, 10, 1, 11, 59))
    assert r.tick(la(2026, 10, 1, 12, 0)) == "started run j1200"
    [(names, auto, note)] = started
    assert names == ["ingest", "score"] and auto is True
    assert "left out feedback: still to run in an earlier run, after studio" in note
    r.stop()


def test_a_step_behind_a_quick_live_step_is_still_waited_for(tmp_path):
    cfg = load_ops_config()
    cfg["lock_path"] = str(tmp_path / "p.lock")
    jobs = JobManager(cfg, tmp_path, db_path=tmp_path / "t.db")
    job = _live_auto_run(jobs, "feedback", done=["ingest", "score", "studio_scan", "studio"])
    # feedback is live and not a slow step: studio_learn comes right after it
    assert jobs.queued_behind({"studio"}) == {}
    # between two steps nothing is live, and nothing can be said to wait behind a slow one
    job.results.append(
        StepResult("feedback", [], la(2026, 10, 1, 7), la(2026, 10, 1, 8), exit_code=0)
    )
    job.active_step = None
    assert jobs.queued_behind({"studio"}) == {}
    assert {"studio_learn"} <= jobs.busy_steps()


def test_a_step_another_run_is_running_is_not_queued_behind_anything(tmp_path):
    cfg = load_ops_config()
    cfg["lock_path"] = str(tmp_path / "p.lock")
    jobs = JobManager(cfg, tmp_path, db_path=tmp_path / "t.db")
    _live_auto_run(jobs, "studio", done=["ingest", "score"])
    assert "studio_learn" in jobs.queued_behind({"studio"})
    # a human's studio_learn_now run holds the studio_learn lock with its live step:
    # studio_learn is busy for real, so the time waits for it
    by_name = {s.name: s for s in jobs.steps()}
    learn = Job(
        id="retry",
        steps=["studio_learn_now"],
        started_at=la(2026, 10, 1, 11),
        plan=[by_name["studio_learn_now"]],
    )
    learn.active_step = "studio_learn_now"
    jobs._live.append(learn)
    behind = jobs.queued_behind({"studio"})
    assert "studio_learn" not in behind and "studio_learn_now" not in behind
    assert "feedback" in behind


def test_publish_is_never_started_even_when_listed(tmp_path):
    steps = [
        Step("ingest", ["python", "run_ingest.py"]),
        Step("publish", ["python", "run_publish.py"]),
        Step("post", ["python", "run_publish.py", "--live"]),
        Step("sneaky", ["python", "-m", "run_publish"]),
    ]
    jobs = FakeJobs(steps)
    r = _runner(tmp_path, jobs, _settings(auto_run_steps=["publish", "post", "sneaky", "ingest"]))
    r.tick(la(2026, 10, 1, 5, 59))
    r.tick(la(2026, 10, 1, 6, 0))
    assert jobs.started == [(["ingest"], True)]
    st = r.status(la(2026, 10, 1, 6, 1))
    assert st.steps == ["ingest"] and set(st.dropped) == {"publish", "post", "sneaky"}


def test_only_one_window_runs_the_timer_and_a_handover_does_not_refire(tmp_path):
    a = _runner(tmp_path, FakeJobs())
    b = _runner(tmp_path, FakeJobs())
    a.tick(la(2026, 10, 1, 5, 30))
    b.tick(la(2026, 10, 1, 5, 31))
    assert b.reason == panel_autorun.REASON_FOLLOWER
    a.tick(la(2026, 10, 1, 6, 0))
    b.tick(la(2026, 10, 1, 6, 0))
    assert len(a.jobs.started) == 1 and b.jobs.started == []
    a.stop()  # window A closes: B takes over without re-running 06:00
    b.tick(la(2026, 10, 1, 6, 10))
    assert b._held is not None and b.jobs.started == []
    b.tick(la(2026, 10, 1, 12, 0))
    assert len(b.jobs.started) == 1
    b.stop()


def test_stop_means_no_run_starts_after_it(tmp_path):
    r = _runner(tmp_path)
    r.tick(la(2026, 10, 1, 5, 59))
    r.stop()
    assert r.tick(la(2026, 10, 1, 6, 0)) == panel_autorun.REASON_STOPPED
    assert r.jobs.started == []
    assert not (tmp_path / "p.lock.autorun").exists() or r._held is None


def test_a_broken_time_list_is_shown_not_run(tmp_path):
    r = _runner(tmp_path, settings=_settings(auto_run_times=["06:00", "bogus"]))
    assert "auto_run_times" in r.tick(la(2026, 10, 1, 6))
    assert r.status(la(2026, 10, 1, 6)).error


def test_the_thread_ticks_and_stops(tmp_path):
    r = _runner(tmp_path, settings=_settings(auto_run_enabled=False))
    r.poll_seconds = 0.05
    r._start_thread()
    deadline = time.monotonic() + 5
    while r.last is None and time.monotonic() < deadline:
        time.sleep(0.02)
    assert r.last is not None and r._thread.name == "autorun"
    r.stop()
    assert r._thread is None


def test_the_autorunner_never_reaches_the_publisher():
    src = (REPO / "panel" / "autorun.py").read_text(encoding="utf-8")
    code = "\n".join(ln for ln in src.splitlines() if not ln.lstrip().startswith("#"))
    for name in ("start_publish_now", "_launch_publish", "PUBLISH_CLI", "run_publish"):
        assert name not in code.split('"""', 2)[-1]


# --------------------------------------------------------------------------- health


def test_sources_are_not_stale_overnight_between_runs():
    now = datetime(2026, 10, 1, 13, 0, tzinfo=UTC)  # 06:00 PDT, 12h after the 18:00 run
    runs = [
        SourceRun("rss_half_hour", cadence_minutes=30, last_run_at=now - timedelta(hours=12)),
        SourceRun("rss_daily", cadence_minutes=1440, last_run_at=now - timedelta(hours=12)),
    ]
    th = Thresholds.from_config(load_ops_config()["health"])
    assert check_sources(runs, now, th).status == "ok"
    assert check_sources(runs, now, Thresholds()).status != "ok"  # the old 30-minute rule


def test_the_shipped_staleness_limits_cover_the_longest_gap_between_runs():
    cfg = load_ops_config()
    times = autorun.parse_times(cfg["auto_run_times"])
    mins = [int(t[:2]) * 60 + int(t[3:]) for t in times]
    gaps = [b - a for a, b in zip(mins, mins[1:], strict=False)] + [mins[0] + 1440 - mins[-1]]
    longest_h = max(gaps) / 60
    th = Thresholds.from_config(cfg["health"])
    assert th.max_hours_since_ingest > longest_h
    assert th.max_hours_since_score > longest_h
    assert th.source_stale_min_hours > longest_h


# --------------------------------------------------------------------------- the pages


@pytest.fixture
def panel_client(db_file, tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from panel import app as panel_app

    cfg_copy = tmp_path / "ops_config.yaml"
    cfg_copy.write_text((REPO / "ops" / "config.yaml").read_text(encoding="utf-8"), "utf-8")
    monkeypatch.setattr(
        panel_app, "save_auto_run", lambda enabled, times: save_auto_run(enabled, times, cfg_copy)
    )
    monkeypatch.setattr(panel_app.AUTO, "_settings", lambda: load_ops_config(cfg_copy))
    return TestClient(panel_app.app, follow_redirects=False), cfg_copy


def test_the_runs_page_shows_and_saves_the_automatic_runs(panel_client):
    client, cfg_copy = panel_client
    body = client.get("/runs").text
    assert "Automatic runs" in body and 'action="/runs/auto"' in body
    assert (
        "01:00, 03:00, 06:00, 09:32, 12:00, 15:00" in body
        and "Publishing never runs on its own" in body
    )
    r = client.post("/runs/auto", data={"times": "7:00, 19:00"})  # box unticked: off
    assert r.status_code == 303 and r.headers["location"] == "/runs?saved=auto"
    saved = load_ops_config(cfg_copy)
    assert saved["auto_run_enabled"] is False and saved["auto_run_times"] == ["07:00", "19:00"]
    assert saved["auto_run_steps"] == SHIPPED_AUTO_STEPS  # the step list is never written
    body = client.get("/runs?saved=auto").text
    assert "Automatic runs saved" in body and "Nothing runs on its own" in body
    r = client.post("/runs/auto", data={"enabled": "1", "times": "07:00, 07:30"})
    assert r.status_code == 303 and "error=" in r.headers["location"]
    assert load_ops_config(cfg_copy)["auto_run_times"] == ["07:00", "19:00"]  # untouched


def test_the_dashboard_says_whether_runs_are_automatic(panel_client):
    client, _ = panel_client
    body = client.get("/").text
    assert "Automatic runs:" in body and "next" in body


def test_closing_the_window_stops_the_timer_before_cancelling_runs(monkeypatch):
    import run_desktop
    from panel import app as panel_app

    order = []
    monkeypatch.setattr(panel_app.AUTO, "stop", lambda: order.append("timer"))
    monkeypatch.setattr(panel_app.JOBS, "cancel", lambda reason: order.append("runs") or False)
    run_desktop.stop_run()
    assert order == ["timer", "runs"]


# --------------------------------------------------------------------------- the daily backup


def test_backup_due_is_daily_and_can_be_off():
    now = la(2026, 10, 1, 6)
    assert autorun.backup_due(None, now, 20)
    assert autorun.backup_due(now - timedelta(hours=21), now, 20)
    assert not autorun.backup_due(now - timedelta(hours=6), now, 20)
    assert not autorun.backup_due(None, now, 0)


def _backing_runner(tmp_path, settings=None, fail=False):
    taken: list[datetime] = []

    def backup(now):
        if fail:
            raise OSError("disk full")
        taken.append(now)
        return tmp_path / f"pipeline-{len(taken)}.sqlite"

    r = AutoRunner(
        FakeJobs(),
        tmp_path / "p.lock.autorun",
        settings=lambda: settings or _settings(auto_run_backup_hours=20),
        tz=lambda: LA,
        backup=backup,
        latest_backup=lambda: taken[-1] if taken else None,
    )
    return r, taken


def test_the_first_automatic_run_each_day_backs_up(tmp_path):
    r, taken = _backing_runner(tmp_path)
    r.tick(la(2026, 10, 1, 5, 59))
    r.tick(la(2026, 10, 1, 6, 0))
    assert taken == [la(2026, 10, 1, 6)] and len(r.jobs.started) == 1
    assert r.outcomes[0].text == "backup saved: pipeline-1.sqlite"
    r.tick(la(2026, 10, 1, 12, 0))
    r.tick(la(2026, 10, 1, 18, 0))
    assert len(taken) == 1 and len(r.jobs.started) == 3  # one backup a day
    r.tick(la(2026, 10, 2, 6, 0))
    assert len(taken) == 2
    assert r.status(la(2026, 10, 2, 6, 1)).backup_hours == 20
    r.stop()


def test_no_backup_without_a_run_when_off_or_when_it_fails(tmp_path):
    r, taken = _backing_runner(tmp_path, _settings(auto_run_backup_hours=0))
    r.tick(la(2026, 10, 1, 5, 59))
    r.tick(la(2026, 10, 1, 6, 0))
    assert taken == [] and len(r.jobs.started) == 1
    r.stop()
    r, taken = _backing_runner(
        tmp_path, _settings(auto_run_backup_hours=20, auto_run_enabled=False)
    )
    r.tick(la(2026, 10, 1, 5, 59))
    r.tick(la(2026, 10, 1, 6, 0))
    assert taken == []  # no run, no backup
    r, _ = _backing_runner(tmp_path, fail=True)
    r.tick(la(2026, 10, 1, 5, 59))
    r.tick(la(2026, 10, 1, 6, 0))
    assert len(r.jobs.started) == 1  # the run is not held back by a failed backup
    assert r.outcomes[0].text == "backup failed: disk full"
    r.stop()


def test_a_backup_cut_short_never_looks_like_the_newest(tmp_path):
    import sqlite3

    from ops import backup as ops_backup

    db = tmp_path / "pipeline.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE t (x)")
    conn.commit()
    conn.close()
    dest = tmp_path / "backups"
    (dest).mkdir()
    (dest / "pipeline-20261001T130000Z.sqlite.part").write_text("half", encoding="utf-8")
    path = ops_backup.backup(db, dest, keep=3)
    assert path.suffix == ".sqlite" and path.exists()
    assert not path.with_name(path.name + ".part").exists()  # renamed, not left behind
    latest = ops_backup.latest_backup(dest)
    assert latest is not None and latest[0] == path


def test_the_shipped_config_backs_up_daily():
    cfg = load_ops_config()
    s = autorun.settings_of(cfg)
    assert 0 < s["backup_hours"] < 24
    assert Thresholds.from_config(cfg["health"]).backup_max_age_hours > 24
