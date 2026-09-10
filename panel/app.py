"""The control panel: one FastAPI app over the whole pipeline.

Routes owned here:
  GET  /                dashboard: health checks, per-step last run, counts, backup, disk
  GET  /sources         every configured ingest source with its freshness and last error
  GET  /feed            scored clusters of the last N hours (what digest.py prints)
  POST /feed/{id}/rate  save a 1-5 human rating and note for a cluster
  GET  /publishing      the schedule, what has posted, and anything needing a human
  GET  /feedback        follower trend, per-post metrics, and the latest report's proposals
  GET  /runs            recent runs started from the panel, with per-step logs
  GET  /runs/current    HTML fragment for the in-page poll while a run is in flight
  POST /runs            start a run of the selected orchestrator steps

The step 2 approval queue's routes are included unchanged (/queue, /drafts/..., /voice),
so the operator has one URL for the whole workflow. Everything else this app shows is
read through `ops/store.py`'s read-only adapters; it owns no tables of its own.

Read-only by design: the panel never edits config.yaml, voice.md or a draft's text, and
has no publish button. Publishing stays a disabled step in ops/config.yaml plus the
env var and the flag, exactly as before.
"""

from __future__ import annotations

import logging
import shutil
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import parse_qs, urlencode

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from jinja2 import ChoiceLoader, FileSystemLoader

import config as root_config
import run_ops
from approval_queue import app as queue_app
from db import Database
from ops import backup as ops_backup
from ops import store as ops_store
from ops.config import load_ops_config
from ops.health import Thresholds
from panel import feed, views
from panel.frozen import data_dir, step_interpreter
from panel.jobs import JobError, JobManager

log = logging.getLogger(__name__)

TEMPLATES_DIR = Path(__file__).with_name("templates")
QUEUE_TEMPLATES_DIR = Path(queue_app.__file__).with_name("templates")

templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
# The queue's base.html is the one layout: the panel's pages extend it so both halves
# of the app share a nav and a stylesheet.
templates.env.loader = ChoiceLoader(
    [FileSystemLoader(str(TEMPLATES_DIR)), FileSystemLoader(str(QUEUE_TEMPLATES_DIR))]
)
templates.env.globals["HAS_PANEL"] = True

app = FastAPI(title="Pipeline control panel")

CONFIG = load_ops_config()
# Steps run in the data directory (the repo root, or the exe's folder in the desktop
# build) under the interpreter that can run them there (see panel/frozen.py).
JOBS = JobManager(CONFIG, data_dir(), python=step_interpreter())


def _now() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def get_conn() -> Iterator[ops_store.sqlite3.Connection]:
    conn = ops_store.connect()
    try:
        yield conn
    finally:
        conn.close()


Conn = Annotated[ops_store.sqlite3.Connection, Depends(get_conn)]


async def read_form(request: Request) -> dict[str, list[str]]:
    """Parse an urlencoded body without pulling in a multipart dependency (as the queue does)."""
    raw = (await request.body()).decode("utf-8")
    return parse_qs(raw, keep_blank_values=True)


# ---------------------------------------------------------------------------
# dashboard
# ---------------------------------------------------------------------------


@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request, conn: Conn):
    now = _now()
    db_path = ops_store.db_path()
    report = run_ops.build_report(conn, CONFIG, db_path, now)
    last_runs = ops_store.last_run_per_step(conn)
    latest_backup = ops_backup.latest_backup(CONFIG["backups"]["dir"])
    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "overall": report.overall,
            "checked_at": report.checked_at,
            "checks": views.check_rows(report),
            "steps": views.step_rows(JOBS.steps(), last_runs, now),
            "counts": ops_store.table_counts(conn),
            "db_path": str(db_path),
            "db_size_mb": db_path.stat().st_size / (1024 * 1024) if db_path.exists() else 0.0,
            "disk_free_mb": _disk_free_mb(db_path),
            "backup_name": latest_backup[0].name if latest_backup else None,
            "backup_age": views.fmt_age(now, latest_backup[1]) if latest_backup else "none yet",
            "job": JOBS.current(),
        },
    )


def _disk_free_mb(db_path: Path) -> float | None:
    try:
        where = db_path.parent if db_path.parent.exists() else Path(".")
        return shutil.disk_usage(where).free / (1024 * 1024)
    except OSError:
        return None


@app.get("/sources", response_class=HTMLResponse)
def sources(request: Request, conn: Conn):
    now = _now()
    thresholds = Thresholds.from_config(CONFIG.get("health"))
    rows = views.source_rows(
        ops_store.fetch_source_runs(conn), now, thresholds.source_stale_multiplier
    )
    return templates.TemplateResponse(
        request,
        "sources.html",
        {
            "rows": rows,
            "enabled": sum(1 for r in rows if r["enabled"]),
            "failing": sum(1 for r in rows if r["status"] in ("fail", "warn")),
        },
    )


# ---------------------------------------------------------------------------
# feed (step 1: what digest.py prints, with its rating prompt inline)
# ---------------------------------------------------------------------------


@contextmanager
def open_db() -> Iterator[Database]:
    """Step 1's own API. The feed page reads and rates clusters exactly as digest.py does.

    Opened inside the route rather than as a dependency: `db.Database` keeps its sqlite
    connection to one thread, and a dependency can run on a different one from the route.
    """
    database = Database(str(ops_store.db_path()))
    try:
        yield database
    finally:
        database.close()


@app.get("/feed", response_class=HTMLResponse)
def feed_page(
    request: Request,
    hours: int | None = None,
    top: int | None = None,
    all: bool = False,
    saved: int | None = None,
):
    settings = feed.feed_settings(root_config.load_config())
    hours = hours or settings["hours"]
    top_n = top or settings["top_n"]
    min_total = 0 if all else settings["threshold"]
    with open_db() as database:
        rows = feed.fetch_entries(database, hours=hours, top_n=top_n, min_total=min_total)
        entries = feed.entry_views(database, rows)
    return templates.TemplateResponse(
        request,
        "feed.html",
        {
            "entries": entries,
            "hours": hours,
            "top_n": top_n,
            "min_total": min_total,
            "show_all": all,
            "labels": feed.RATING_LABELS,
            "saved": saved,
        },
    )


@app.post("/feed/{cluster_id}/rate")
async def rate_cluster(cluster_id: int, request: Request):
    """Save a human 1-5 rating: the ground truth step 4 tunes the rubric against."""
    form = await read_form(request)
    try:
        rating = feed.parse_rating(_first(form, "rating"))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    note = (_first(form, "note") or "").strip() or None
    with open_db() as database:
        if database.get_cluster(cluster_id) is None:
            raise HTTPException(status_code=404, detail="no such cluster")
        database.insert_rating(cluster_id, rating, note)
    query = _first(form, "back") or ""
    return RedirectResponse(f"/feed?{query}&saved={cluster_id}".lstrip("&"), status_code=303)


def _first(form: dict[str, list[str]], key: str) -> str | None:
    values = form.get(key) or []
    return values[0] if values else None


# ---------------------------------------------------------------------------
# publishing and feedback (read-only views of steps 3 and 4)
# ---------------------------------------------------------------------------


@app.get("/publishing", response_class=HTMLResponse)
def publishing(request: Request, conn: Conn):
    now = _now()
    state = ops_store.fetch_publish_state(conn, now)
    rows = ops_store.fetch_publish_rows(conn)
    activity = ops_store.fetch_stage_activity(conn, now)
    publish_step = next((s for s in JOBS.steps() if s.name == "publish"), None)
    return templates.TemplateResponse(
        request,
        "publishing.html",
        {
            "state": state,
            "posts": rows["posts"],
            "needs_attention": rows["needs_attention"],
            "approved_waiting": activity.approved_drafts,
            "last_posted": views.fmt_age(now, state.latest_posted_at),
            "publish_enabled": bool(publish_step and publish_step.enabled),
        },
    )


@app.get("/feedback", response_class=HTMLResponse)
def feedback_page(request: Request, conn: Conn):
    now = _now()
    series = ops_store.fetch_follower_series(conn)
    return templates.TemplateResponse(
        request,
        "feedback.html",
        {
            "series": series,
            "spark": views.sparkline([s["followers"] for s in series]),
            "growth": views.series_growth(series),
            "posts": ops_store.fetch_post_metrics(conn),
            "report": ops_store.fetch_latest_feedback_report(conn),
            "state": ops_store.fetch_feedback_state(conn),
            "now": now,
        },
    )


# ---------------------------------------------------------------------------
# runs
# ---------------------------------------------------------------------------


@app.get("/runs", response_class=HTMLResponse)
def runs(request: Request, error: str | None = None):
    return templates.TemplateResponse(
        request,
        "runs.html",
        {"jobs": _job_views(JOBS.history()), "steps": JOBS.steps(), "error": error},
    )


@app.get("/runs/current", response_class=HTMLResponse)
def runs_current(request: Request):
    """Fragment polled by the runs page while a job is in flight."""
    return templates.TemplateResponse(request, "_jobs.html", {"jobs": _job_views(JOBS.history())})


@app.post("/runs")
async def start_run(request: Request):
    form = await read_form(request)
    try:
        JOBS.start(form.get("step", []))
    except JobError as exc:
        return RedirectResponse(f"/runs?{urlencode({'error': str(exc)})}", status_code=303)
    return RedirectResponse("/runs", status_code=303)


def _job_views(jobs: list[Any]) -> list[dict[str, Any]]:
    now = _now()
    out = []
    for job in jobs:
        out.append(
            {
                "id": job.id,
                "state": job.state,
                "running": job.running,
                "steps": job.steps,
                "started": views.fmt_age(now, job.started_at),
                "duration": views.fmt_duration(job.duration_seconds),
                "error": job.error,
                "results": [
                    {
                        "name": r.name,
                        "status": "skip" if r.skipped else ("ok" if r.ok else "fail"),
                        "outcome": _result_outcome(r),
                        "duration": views.fmt_duration(r.duration_seconds),
                        "stdout": r.stdout_tail,
                        "stderr": r.stderr_tail,
                    }
                    for r in job.results
                ],
            }
        )
    return out


def _result_outcome(result: Any) -> str:
    if result.skipped:
        return f"skipped ({result.skipped_reason})"
    if result.timed_out:
        return "timed out"
    return f"exit {result.exit_code}"


# The approval queue's routes, unchanged, on the same app: same paths, same handlers,
# so /queue and /drafts/{id} behave exactly as they do under run_queue.py. FastAPI's
# own generated endpoints are left behind — this app already has its own.
_GENERATED = {"/openapi.json", "/docs", "/docs/oauth2-redirect", "/redoc"}


def _adopt_queue_routes() -> None:
    have = {getattr(r, "path", None) for r in app.router.routes}
    for route in queue_app.app.router.routes:
        path = getattr(route, "path", None)
        if path in _GENERATED or path in have:
            continue
        app.router.routes.append(route)


_adopt_queue_routes()
