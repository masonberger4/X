"""Server-rendered approval queue: FastAPI + Jinja2, no JavaScript framework.

Routes:
  GET  /                      pending drafts (source, score, rationale)
  GET  /drafts/{id}           detail: single_post, thread, claims_to_verify, edit form
  POST /drafts/{id}/approve
  POST /drafts/{id}/edit      saves edited text (+ approves unless 'keep_pending' is set)
  POST /drafts/{id}/reject
  POST /drafts/{id}/snooze    hides the draft for 24h
  GET  /status/{status}       approved / rejected / snoozed / failed lists

Form bodies are parsed with urllib so no multipart dependency is needed.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from pathlib import Path
from typing import Annotated
from urllib.parse import parse_qs

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from approval_queue import store
from draft.schema import MAX_POST_CHARS, tweet_length

log = logging.getLogger(__name__)

TEMPLATES_DIR = Path(__file__).with_name("templates")
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
templates.env.filters["tweet_length"] = tweet_length
templates.env.globals["MAX_POST_CHARS"] = MAX_POST_CHARS

app = FastAPI(title="Approval queue")


def get_conn() -> Iterator[store.sqlite3.Connection]:
    conn = store.connect()
    try:
        yield conn
    finally:
        conn.close()


Conn = Annotated[store.sqlite3.Connection, Depends(get_conn)]


async def read_form(request: Request) -> dict[str, str]:
    """Parse an application/x-www-form-urlencoded body; last value wins for repeated keys."""
    body = (await request.body()).decode("utf-8")
    return {k: v[-1] for k, v in parse_qs(body, keep_blank_values=True).items()}


def _split_thread(text: str) -> list[str]:
    """Thread posts are separated by a line containing only '---'."""
    posts = [p.strip() for p in text.replace("\r\n", "\n").split("\n---\n")]
    return [p for p in posts if p]


def _note(form: dict[str, str]) -> str | None:
    note = form.get("note", "").strip()
    return note or None


@app.get("/", response_class=HTMLResponse)
def index(request: Request, conn: Conn):
    drafts = store.list_drafts(conn, store.STATUS_PENDING)
    return templates.TemplateResponse(
        request, "index.html", {"drafts": drafts, "status": "pending"}
    )


@app.get("/status/{status}", response_class=HTMLResponse)
def by_status(status: str, request: Request, conn: Conn):
    if status not in store.STATUSES:
        raise HTTPException(404, "unknown status")
    drafts = store.list_drafts(conn, status)
    return templates.TemplateResponse(request, "index.html", {"drafts": drafts, "status": status})


@app.get("/drafts/{draft_id}", response_class=HTMLResponse)
def detail(draft_id: int, request: Request, conn: Conn):
    row = store.get_draft(conn, draft_id)
    if row is None:
        raise HTTPException(404, "no such draft")
    decisions = store.list_decisions(conn, draft_id)
    return templates.TemplateResponse(
        request,
        "detail.html",
        {"d": row, "decisions": decisions, "thread_text": "\n---\n".join(row.draft.thread)},
    )


def _redirect_home() -> RedirectResponse:
    return RedirectResponse("/", status_code=303)


@app.post("/drafts/{draft_id}/approve")
async def approve(draft_id: int, request: Request, conn: Conn):
    form = await read_form(request)
    try:
        store.approve(conn, draft_id, note=_note(form))
    except KeyError as exc:
        raise HTTPException(404, "no such draft") from exc
    log.info("draft %d approved", draft_id)
    return _redirect_home()


@app.post("/drafts/{draft_id}/edit")
async def edit(draft_id: int, request: Request, conn: Conn):
    form = await read_form(request)
    single_post = form.get("single_post", "").strip()
    thread = _split_thread(form.get("thread", ""))
    if not single_post:
        raise HTTPException(400, "single_post cannot be empty")
    over = [p for p in [single_post, *thread] if tweet_length(p) > MAX_POST_CHARS]
    if over:
        raise HTTPException(400, f"{len(over)} post(s) exceed {MAX_POST_CHARS} characters")
    try:
        store.edit(
            conn,
            draft_id,
            single_post=single_post,
            thread=thread,
            note=_note(form),
            approve_after="keep_pending" not in form,
        )
    except KeyError as exc:
        raise HTTPException(404, "no such draft") from exc
    log.info("draft %d edited", draft_id)
    if "keep_pending" in form:
        return RedirectResponse(f"/drafts/{draft_id}", status_code=303)
    return _redirect_home()


@app.post("/drafts/{draft_id}/reject")
async def reject(draft_id: int, request: Request, conn: Conn):
    form = await read_form(request)
    try:
        store.reject(conn, draft_id, note=_note(form))
    except KeyError as exc:
        raise HTTPException(404, "no such draft") from exc
    log.info("draft %d rejected", draft_id)
    return _redirect_home()


@app.post("/drafts/{draft_id}/snooze")
async def snooze(draft_id: int, request: Request, conn: Conn):
    form = await read_form(request)
    try:
        store.snooze(conn, draft_id, note=_note(form))
    except KeyError as exc:
        raise HTTPException(404, "no such draft") from exc
    log.info("draft %d snoozed for %dh", draft_id, store.SNOOZE_HOURS)
    return _redirect_home()
