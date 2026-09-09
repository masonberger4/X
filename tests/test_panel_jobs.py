"""The panel runs orchestrator steps the same way cron does, and refuses everything else."""

import sys
import time

import pytest

from ops import lock, store
from panel.jobs import STATE_DONE, STATE_LOCKED, JobError, JobManager


def _cfg(tmp_path, steps):
    return {"steps": steps, "lock_path": str(tmp_path / "p.lock"), "run_log_tail_chars": 4000}


def _step(name, code, enabled=True):
    return {
        "name": name,
        "argv": ["python", "-c", code],
        "enabled": enabled,
        "required": False,
        "timeout_seconds": 30,
    }


def _wait(job, timeout=20.0):
    deadline = time.monotonic() + timeout
    while job.running and time.monotonic() < deadline:
        time.sleep(0.02)
    assert not job.running, "job did not finish"
    return job


@pytest.fixture
def manager(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "executable", sys.executable)
    cfg = _cfg(
        tmp_path,
        [
            _step("ok", "print('hello')"),
            _step("boom", "import sys; sys.stderr.write('bad\\n'); sys.exit(3)"),
            _step("off", "print('never')", enabled=False),
        ],
    )
    return JobManager(cfg, tmp_path.parent, db_path=tmp_path / "t.db")


def test_a_run_records_each_step_in_pipeline_runs(manager, tmp_path):
    job = _wait(manager.start(["ok"]))
    assert job.state == STATE_DONE and job.failed_steps == []
    assert job.results[0].exit_code == 0 and "hello" in job.results[0].stdout_tail
    conn = store.connect(tmp_path / "t.db")
    try:
        rows = conn.execute("SELECT run_id, step, exit_code FROM pipeline_runs").fetchall()
    finally:
        conn.close()
    assert [(r["step"], r["exit_code"]) for r in rows] == [("ok", 0)]
    assert "panel" in rows[0]["run_id"], "a panel run is distinguishable from a cron run"


def test_a_failing_step_is_reported_not_raised(manager):
    job = _wait(manager.start(["boom"]))
    assert job.state == STATE_DONE and job.failed_steps == ["boom"]
    assert "bad" in job.results[0].stderr_tail


def test_a_disabled_step_is_skipped_never_run(manager):
    job = _wait(manager.start(["off"]))
    assert job.results[0].skipped_reason == "disabled"
    assert job.results[0].stdout_tail == ""


def test_steps_run_in_config_order_whatever_the_form_says(manager):
    job = _wait(manager.start(["off", "ok"]))
    assert [r.name for r in job.results] == ["ok", "off"]


def test_unknown_and_empty_selections_are_refused(manager):
    with pytest.raises(JobError, match="unknown step"):
        manager.start(["run_publish.py"])
    with pytest.raises(JobError, match="at least one"):
        manager.start([" "])


def test_a_step_carrying_the_live_flag_is_refused(tmp_path):
    cfg = _cfg(tmp_path, [_step("publish", "print(1)")])
    cfg["steps"][0]["argv"].append("--live")
    manager = JobManager(cfg, tmp_path.parent, db_path=tmp_path / "t.db")
    with pytest.raises(JobError, match="--live"):
        manager.start(["publish"])


def test_a_held_pipeline_lock_stops_the_run(manager):
    held = lock.acquire(manager.cfg["lock_path"])
    assert held is not None
    try:
        job = _wait(manager.start(["ok"]))
    finally:
        held.release()
    assert job.state == STATE_LOCKED and job.results == []
    assert "lock" in job.error


def test_only_one_job_runs_at_a_time(tmp_path):
    cfg = _cfg(tmp_path, [_step("slow", "import time; time.sleep(1)")])
    manager = JobManager(cfg, tmp_path.parent, db_path=tmp_path / "t.db")
    first = manager.start(["slow"])
    with pytest.raises(JobError, match="already in progress"):
        manager.start(["slow"])
    _wait(first)
    manager.start(["slow"])  # the slot is free again
    _wait(manager.current() or first)
