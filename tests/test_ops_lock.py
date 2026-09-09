"""ops/lock.py: non-blocking single-instance lock with dead-pid reclaim."""

import json
import logging
import os
import subprocess
import sys

from ops import lock


def test_acquire_then_second_acquire_returns_none(tmp_path):
    path = tmp_path / "pipeline.lock"
    first = lock.acquire(path)
    assert first is not None
    assert first.pid == os.getpid()
    holder = json.loads(path.read_text())
    assert holder["pid"] == os.getpid()
    assert "started_at" in holder

    assert lock.acquire(path) is None
    first.release()

    again = lock.acquire(path)
    assert again is not None
    again.release()
    assert path.read_text() == ""  # released locks leave an empty file


def test_context_manager_releases(tmp_path):
    path = tmp_path / "pipeline.lock"
    with lock.acquire(path) as held:
        assert held is not None
        assert lock.acquire(path) is None
    assert lock.acquire(path) is not None


def test_dead_pid_is_reclaimed_with_warning(tmp_path, caplog):
    path = tmp_path / "pipeline.lock"
    # Spawn a process, let it exit, use its (now dead) pid.
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    child.wait()
    dead_pid = child.pid
    path.write_text(json.dumps({"pid": dead_pid, "started_at": "2026-01-01T00:00:00+00:00"}))

    with caplog.at_level(logging.WARNING, logger="ops.lock"):
        held = lock.acquire(path)
    assert held is not None
    assert any("reclaiming stale lock" in r.message for r in caplog.records)
    held.release()


def test_live_pid_is_not_reclaimed(tmp_path):
    path = tmp_path / "pipeline.lock"
    # A file naming a live process (a child we keep alive) with no flock held.
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        path.write_text(json.dumps({"pid": child.pid, "started_at": "2026-01-01T00:00:00+00:00"}))
        assert lock.acquire(path) is None
        assert json.loads(path.read_text())["pid"] == child.pid  # untouched
    finally:
        child.kill()
        child.wait()


def test_garbage_lock_file_is_reclaimed(tmp_path):
    path = tmp_path / "pipeline.lock"
    path.write_text("not json")
    held = lock.acquire(path)
    assert held is not None
    held.release()


def test_pid_alive():
    assert lock.pid_alive(os.getpid())
    assert not lock.pid_alive(0)
    assert not lock.pid_alive(-1)
