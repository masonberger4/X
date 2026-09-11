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


def _approved(conn, draft_id):
    queue_store.approve(conn, draft_id)
    return draft_id


def test_the_feed_page_runs_ingest_and_score_with_one_button(client, monkeypatch):
    body = client.get("/feed").text
    assert "Ingest and score" in body and 'value="ingest"' in body and 'value="score"' in body
    started = {}
    monkeypatch.setattr(panel_app.JOBS, "start", lambda steps: started.setdefault("steps", steps))
    r = client.post("/runs", data={"step": ["ingest", "score"], "back": "/feed?hours=24"})
    assert r.status_code == 303 and r.headers["location"] == "/feed?hours=24"
    assert started["steps"] == ["ingest", "score"]
    # an off-site `back` is ignored
    r = client.post("/runs", data={"step": ["ingest"], "back": "//evil.example/x"})
    assert r.headers["location"] == "/runs"


def test_the_pending_page_offers_draft_and_verify_and_shows_the_run_in_flight(client, monkeypatch):
    body = client.get("/queue").text
    assert ">Draft<" in body and ">Verify<" in body and 'value="verify"' in body
    assert "disabled" not in body.split('class="runbar"')[1].split("</div>")[0]
    monkeypatch.setattr(
        panel_app,
        "current_run",
        lambda: {
            "id": "x",
            "steps": ["draft"],
            "active_step": "draft",
            "active_for": "3s",
            "started": "now",
        },
    )
    panel_app._adopt_queue_routes()  # re-register the (patched) global on the queue's env
    body = client.get("/queue").text
    bar = body.split('class="runbar"')[1].split("</div>")[0]
    assert "running" in bar and "watch the log" in bar and "disabled" in bar


def test_the_approved_page_has_publish_now_and_set_schedule(client, conn, draft_id, monkeypatch):
    _approved(conn, draft_id)
    monkeypatch.delenv("PUBLISH_ENABLED", raising=False)
    body = client.get("/status/approved").text
    assert "Publish now" in body and 'action="/publishing/now"' in body
    assert "Set schedule" in body and f'name="order_{draft_id}"' in body
    assert "PUBLISH_ENABLED" in body  # the rehearsal warning
    monkeypatch.setenv("PUBLISH_ENABLED", "1")
    assert "PUBLISH_ENABLED</code> is not" not in client.get("/status/approved").text

    started = {}
    monkeypatch.setattr(
        panel_app.JOBS, "start_publish_now", lambda d: started.setdefault("draft", d)
    )
    r = client.post("/publishing/now", data={"draft_id": str(draft_id), "back": "/status/approved"})
    assert r.status_code == 303 and r.headers["location"] == "/status/approved"
    assert started["draft"] == draft_id
    assert client.post("/publishing/now", data={"draft_id": "x"}).status_code == 400

    def refuse(d):
        raise JobError("a run is already in progress")

    monkeypatch.setattr(panel_app.JOBS, "start_publish_now", refuse)
    r = client.post("/publishing/now", data={"draft_id": "1"})
    assert "already in progress" in unquote_plus(r.headers["location"])


def test_saving_the_order_shows_it_and_feeds_the_publisher(client, conn, draft_id):
    from publish import store as publish_store

    _approved(conn, draft_id)
    seed_item(conn, "i2", source="pubmed")
    d2 = queue_store.insert_draft(
        conn,
        item_id="i2",
        model="m",
        draft=Draft(single_post=f"Two {URL}", thread=[], suggested_visual="", why_it_matters=""),
    )
    _approved(conn, d2)
    r = client.post(
        "/publishing/order",
        data={f"order_{draft_id}": "2", f"order_{d2}": "1", "back": "/status/approved"},
    )
    assert r.status_code == 303 and r.headers["location"] == "/status/approved"
    body = client.get("/status/approved").text
    assert (
        "#1" in body and "#2" in body and f'name="order_{d2}" form="order-form" value="1"' in body
    )
    got = {a.draft_id: a.position for a in publish_store.fetch_approved(conn=conn)}
    assert got == {draft_id: 2, d2: 1}
    assert client.post("/publishing/order", data={f"order_{d2}": "zero"}).status_code == 400
    # blank boxes clear the order
    client.post("/publishing/order", data={f"order_{draft_id}": "", f"order_{d2}": ""})
    assert all(a.position is None for a in publish_store.fetch_approved(conn=conn))


def test_the_standalone_queue_shows_no_run_or_publish_buttons(conn, draft_id):
    from approval_queue import app as queue_app

    _approved(conn, draft_id)
    c = TestClient(queue_app.app)
    queue_app.templates.env.globals["HAS_PANEL"] = False
    queue_app.templates.env.globals["current_run"] = lambda: None
    try:
        for path in ("/queue", "/status/approved"):
            body = c.get(path).text
            assert 'action="/publishing/now"' not in body and 'class="runbar"' not in body
            assert "Set schedule" not in body and 'action="/runs"' not in body
    finally:
        panel_app._adopt_queue_routes()


def test_the_publishing_page_edits_the_two_caps(client, monkeypatch, tmp_path):
    from publish import scheduler as publish_scheduler

    cfg = tmp_path / "publish.yaml"
    cfg.write_text("timezone: UTC\nmax_posts_per_day: 3\nmin_gap_minutes: 90\n")
    monkeypatch.setattr(publish_scheduler, "CONFIG_PATH", cfg)
    body = client.get("/publishing").text
    assert 'name="max_posts_per_day"' in body and 'value="90"' in body
    r = client.post("/publishing/caps", data={"max_posts_per_day": "4", "min_gap_minutes": "45"})
    assert r.status_code == 303 and r.headers["location"] == "/publishing?saved=1"
    assert "min_gap_minutes: 45" in cfg.read_text()
    assert 'value="45"' in client.get("/publishing?saved=1").text
    r = client.post("/publishing/caps", data={"max_posts_per_day": "0", "min_gap_minutes": "45"})
    assert "at least 1" in unquote_plus(r.headers["location"])
    r = client.post("/publishing/caps", data={"max_posts_per_day": "x", "min_gap_minutes": "45"})
    assert "whole number" in unquote_plus(r.headers["location"])
    assert "max_posts_per_day: 4" in cfg.read_text()
