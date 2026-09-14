"""Automatic publishing from the panel: the loop's decision, and what the pages show."""

from datetime import UTC, datetime, timedelta

import pytest

from panel.autopublish import (
    REASON_NOT_LIVE,
    REASON_OFF,
    REASON_STARTED,
    REASON_WAITING,
    AutoPublisher,
)
from panel.jobs import JobError

T0 = datetime(2026, 6, 1, 10, 0, tzinfo=UTC)


class FakeJobs:
    def __init__(self, busy=False):
        self.busy = busy
        self.started = 0

    def start_publish_auto(self):
        if self.busy:
            raise JobError("a run is already in progress")
        self.started += 1


def _auto(jobs, enabled=True, interval=15, live=True):
    cfg = {"auto_publish_enabled": enabled, "auto_publish_interval_minutes": interval}
    return AutoPublisher(jobs, settings=lambda: cfg, live=lambda: live)


def test_off_starts_nothing():
    jobs = FakeJobs()
    auto = _auto(jobs, enabled=False)
    assert auto.tick(T0) == REASON_OFF and jobs.started == 0
    assert not auto.status(T0).enabled and auto.status(T0).next_due is None


def test_on_without_the_env_gate_starts_nothing():
    """A rehearsal every fifteen minutes would only fill the runs page."""
    jobs = FakeJobs()
    auto = _auto(jobs, live=False)
    assert auto.tick(T0) == REASON_NOT_LIVE and jobs.started == 0
    st = auto.status(T0)
    assert st.enabled and not st.live and not st.active


def test_on_starts_a_run_then_waits_for_the_interval():
    jobs = FakeJobs()
    auto = _auto(jobs, interval=15)
    assert auto.tick(T0) == REASON_STARTED and jobs.started == 1
    assert auto.tick(T0 + timedelta(minutes=14)) == REASON_WAITING and jobs.started == 1
    assert auto.status(T0 + timedelta(minutes=14)).next_due == T0 + timedelta(minutes=15)
    assert auto.tick(T0 + timedelta(minutes=15)) == REASON_STARTED and jobs.started == 2


def test_a_run_in_progress_is_retried_on_the_next_poll_not_after_the_interval():
    jobs = FakeJobs(busy=True)
    auto = _auto(jobs)
    assert auto.tick(T0).startswith("skipped") and auto.last_started is None
    jobs.busy = False
    assert auto.tick(T0 + timedelta(seconds=30)) == REASON_STARTED


def test_the_settings_are_re_read_every_tick():
    jobs = FakeJobs()
    cfg = {"auto_publish_enabled": False, "auto_publish_interval_minutes": 15}
    auto = AutoPublisher(jobs, settings=lambda: cfg, live=lambda: True)
    assert auto.tick(T0) == REASON_OFF
    cfg["auto_publish_enabled"] = True
    assert auto.tick(T0) == REASON_STARTED


def test_the_thread_ticks_and_stops(monkeypatch):
    jobs = FakeJobs()
    auto = _auto(jobs)
    auto.poll_seconds = 0.01
    auto.start()
    deadline = datetime.now(UTC) + timedelta(seconds=5)
    while jobs.started == 0 and datetime.now(UTC) < deadline:
        pass
    auto.stop()
    assert jobs.started == 1 and auto._thread is None


@pytest.fixture
def client(db_file, tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from panel import app as panel_app
    from publish import scheduler

    cfg = tmp_path / "publish.yaml"
    cfg.write_text(
        "timezone: America/New_York\nslots: []\nmax_posts_per_day: 3\n"
        "min_gap_minutes: 90  # keep\nauto_publish_enabled: false\n"
        "auto_publish_interval_minutes: 15\n"
    )
    monkeypatch.setattr(scheduler, "CONFIG_PATH", cfg)
    monkeypatch.setattr(panel_app.AUTO, "last_started", None)
    return TestClient(panel_app.app, follow_redirects=False), cfg


def test_the_publishing_page_switches_it_on_and_the_approved_page_says_so(client, monkeypatch):
    c, cfg = client
    body = c.get("/publishing").text
    assert "Automatic publishing" in body and 'name="enabled" value="1"' in body
    assert "automatic publishing" not in c.get("/status/approved").text

    r = c.post("/publishing/auto", data={"enabled": "1", "interval_minutes": "20"})
    assert r.status_code == 303 and r.headers["location"] == "/publishing?saved=1"
    text = cfg.read_text()
    assert "auto_publish_enabled: true\n" in text
    assert "auto_publish_interval_minutes: 20\n" in text
    assert "min_gap_minutes: 90  # keep" in text, "the other lines and comments survive"

    monkeypatch.delenv("PUBLISH_ENABLED", raising=False)
    body = c.get("/status/approved").text
    assert "automatic publishing" in body and "not live" in body
    monkeypatch.setenv("PUBLISH_ENABLED", "1")
    body = c.get("/status/approved").text
    assert "automatic publishing" in body and "next run" in body

    r = c.post("/publishing/auto", data={"enabled": "0", "interval_minutes": "x"})
    assert r.status_code == 303 and "error=" in r.headers["location"]
    assert "auto_publish_enabled: true\n" in cfg.read_text(), "a bad form saves nothing"
    r = c.post("/publishing/auto", data={"enabled": "0", "interval_minutes": "15"})
    assert "auto_publish_enabled: false\n" in cfg.read_text()


def test_the_app_never_runs_the_loop_under_the_test_client(client):
    from panel import app as panel_app

    assert panel_app.AUTO._thread is None
