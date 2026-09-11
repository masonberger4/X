"""The control panel's pages, and the fact that it is one app with the approval queue."""

from urllib.parse import unquote_plus

import pytest
from fastapi.testclient import TestClient

from approval_queue import store as queue_store
from draft.schema import Claim, Draft
from panel import app as panel_app
from panel.jobs import JobError
from tests.conftest import URL, seed_item


@pytest.fixture
def client(db_file):
    # db_file sets DB_PATH, so the panel's per-request connections hit the temp file
    return TestClient(panel_app.app, follow_redirects=False)


@pytest.fixture
def draft_id(conn):
    seed_item(conn, "i1", source="biorxiv")
    d = Draft(
        single_post=f"Preprint: ORR 88%. {URL}",
        thread=["Preprint. one", "two", f"three {URL}"],
        suggested_visual="plot",
        why_it_matters="matters",
        claims_to_verify=[Claim("ORR 88% appears in the abstract", "low")],
    )
    return queue_store.insert_draft(conn, item_id="i1", model="m", draft=d)


def test_dashboard_shows_health_steps_and_counts(client):
    r = client.get("/")
    assert r.status_code == 200
    body = r.text
    assert "Health:" in body
    for step in ("ingest", "score", "draft", "publish"):
        assert step in body
    assert "database" in body and "disk free" in body


def test_dashboard_marks_a_disabled_step(client):
    body = client.get("/").text
    # feedback is the step shipped disabled; the steps table is below the checks table
    row = body.rsplit("<strong>feedback</strong>", 1)[1].split("</tr>")[0]
    assert "disabled" in row
    assert 'value="feedback"' not in row, "a disabled step has no run checkbox"
    # publish is shipped enabled (a dry run) and so gets a run checkbox
    assert 'value="publish"' in body and 'value="feedback"' not in body


def test_sources_page_lists_configured_sources(client):
    r = client.get("/sources")
    assert r.status_code == 200
    assert "enabled" in r.text and "Cadence" in r.text


def test_runs_page_offers_the_configured_steps(client):
    body = client.get("/runs").text
    assert 'value="ingest"' in body
    assert "No runs started from the panel yet" in body


def test_starting_a_run_redirects_and_a_refused_one_says_why(client, monkeypatch):
    started = {}
    monkeypatch.setattr(panel_app.JOBS, "start", lambda steps: started.setdefault("steps", steps))
    r = client.post("/runs", data={"step": ["ingest", "score"]})
    assert r.status_code == 303 and r.headers["location"] == "/runs"
    assert started["steps"] == ["ingest", "score"]

    def refuse(steps):
        raise JobError("a run is already in progress")

    monkeypatch.setattr(panel_app.JOBS, "start", refuse)
    r = client.post("/runs", data={"step": ["ingest"]})
    assert r.status_code == 303
    assert "already in progress" in unquote_plus(r.headers["location"])


def test_the_run_fragment_reports_whether_a_run_is_in_flight(client):
    assert 'data-running="0"' in client.get("/runs/current").text


def test_the_approval_queue_is_part_of_the_same_app(client, conn, draft_id):
    assert client.get("/").status_code == 200  # the dashboard, not the queue
    assert f"/drafts/{draft_id}" in client.get("/queue").text
    assert "Preprint: ORR 88%." in client.get(f"/drafts/{draft_id}").text
    assert client.get("/voice").status_code == 200
    r = client.post(f"/drafts/{draft_id}/approve", data={"note": "good"})
    assert r.status_code == 303 and r.headers["location"] == "/queue"
    assert queue_store.get_draft(conn, draft_id).status == "approved"


def test_the_nav_links_both_halves_together(client):
    body = client.get("/").text
    for link in ('href="/"', 'href="/sources"', 'href="/runs"', 'href="/queue"', 'href="/voice"'):
        assert link in body


def test_the_panel_has_no_publish_button(client):
    for path in ("/", "/runs", "/sources"):
        body = client.get(path).text
        assert "--live" not in body
        assert "PUBLISH_ENABLED" not in body


def test_the_queue_pages_keep_the_panel_links_in_the_nav(client):
    """The queue renders with its own template env; inside the panel it must still link back."""
    for path in ("/queue", "/status/approved", "/voice"):
        body = client.get(path).text
        assert 'href="/"' in body and 'href="/feed"' in body and 'href="/runs"' in body, path


def test_a_long_source_error_wraps_instead_of_widening_the_table(client, conn):
    conn.execute(
        "CREATE TABLE IF NOT EXISTS source_runs (source TEXT PRIMARY KEY, last_run_at TEXT,"
        " fetched INTEGER, inserted INTEGER, error TEXT)"
    )
    long_url = "https://example.org/api?" + "&".join(f"p{i}=v{i}" for i in range(60))
    conn.execute(
        "INSERT OR REPLACE INTO source_runs VALUES ('clinicaltrials_oncology', ?, 0, 0, ?)",
        ("2026-09-10T00:00:00+00:00", f"Client error '403 Forbidden' for url '{long_url}'"),
    )
    conn.commit()
    body = client.get("/sources").text
    cell = body.split("403 Forbidden")[0].rsplit("<td", 1)[1]
    assert "wrap" in cell


def test_the_runs_page_offers_stop_while_running_and_the_route_cancels(client, monkeypatch):
    from panel.jobs import Job

    running = Job(id="abc", steps=["ingest"], started_at=panel_app._now())
    monkeypatch.setattr(panel_app.JOBS, "history", lambda: [running])
    body = client.get("/runs/current").text
    assert 'action="/runs/cancel"' in body and "Stop this run" in body

    called = {}
    monkeypatch.setattr(panel_app.JOBS, "cancel", lambda *a: called.setdefault("yes", True))
    r = client.post("/runs/cancel")
    assert r.status_code == 303 and r.headers["location"] == "/runs" and called["yes"]


def test_the_run_fragment_shows_finished_steps_and_the_live_one_mid_run(client, monkeypatch):
    from ops.runner import StepResult
    from panel.jobs import Job

    now = panel_app._now()
    running = Job(id="abc", steps=["ingest", "score", "draft"], started_at=now)
    running.results = [StepResult("ingest", ["x"], now, now, exit_code=0, stdout_tail="12 new")]
    running.active_step, running.active_since = "score", now
    monkeypatch.setattr(panel_app.JOBS, "history", lambda: [running])
    body = client.get("/runs/current").text
    assert "12 new" in body, "a finished step's log is shown before the run ends"
    assert "<strong>score</strong>" in body and "then draft" in body
    assert "running…" not in body
