"""Server-rendered approval queue: FastAPI + Jinja2, no JavaScript framework.

Routes:
  GET  /                      pending drafts (source, score, rationale)
  GET  /drafts/{id}           detail: single_post, thread, claims_to_verify (+ step 2b
                              verdicts with source links), edit form
  POST /drafts/{id}/approve   refused with 409 while a claim is contradicted, unless the
                              form carries override=1
  POST /drafts/{id}/edit      saves edited text (+ approves unless 'keep_pending' is set)
  POST /drafts/{id}/reject
  POST /drafts/{id}/snooze    hides the draft for 24h
  GET  /status/{status}       approved / rejected / snoozed / failed lists
  GET  /voice                 voice report (step 7) from live data; ?weeks=N

Step 7: the edit and reject forms take an optional category (why the draft was edited or
rejected); the detail page shows a before/after diff for every edit.

Form bodies are parsed with urllib so no multipart dependency is needed.
"""

from __future__ import annotations

import difflib
import logging
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated
from urllib.parse import parse_qs

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from approval_queue import store
from draft.examples import parse_decision_text
from draft.prompt import VOICE_PATH
from draft.schema import MAX_POST_CHARS, tweet_length
from draft.settings import load_draft_config
from draft.voice_report import build_report
from verify import store as verify_store

log = logging.getLogger(__name__)

TEMPLATES_DIR = Path(__file__).with_name("templates")
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
templates.env.filters["tweet_length"] = tweet_length
templates.env.globals["MAX_POST_CHARS"] = MAX_POST_CHARS
templates.env.globals["DECISION_CATEGORIES"] = store.DECISION_CATEGORIES

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


def _category(form: dict[str, str]) -> str | None:
    """Optional decision category from the form; blank -> None, unknown -> 400."""
    try:
        return store.validate_category(form.get("category"))
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


def _post_lines(text: str | None) -> list[str]:
    """A decision text (JSON or bare post) as display lines: single post, then thread posts."""
    single, thread = parse_decision_text(text)
    lines = [f"single: {single}"]
    lines.extend(f"thread {i}: {p}" for i, p in enumerate(thread, 1))
    return lines


def decision_diff(original_text: str | None, edited_text: str | None) -> list[tuple[str, str]]:
    """Line-level before/after diff as (css_class, line) pairs: 'del', 'add' or ''."""
    out: list[tuple[str, str]] = []
    for line in difflib.ndiff(_post_lines(original_text), _post_lines(edited_text)):
        tag, body = line[:2], line[2:]
        if tag == "- ":
            out.append(("del", body))
        elif tag == "+ ":
            out.append(("add", body))
        elif tag == "  ":
            out.append(("", body))
        # '? ' hint lines are dropped
    return out


def _decision_views(decisions: list[store.sqlite3.Row]) -> list[dict]:
    views = []
    for x in decisions:
        keys = x.keys()
        edited = x["edited_text"] if "edited_text" in keys else None
        diff = None
        if x["action"] == store.ACTION_EDIT and edited and edited != x["original_text"]:
            diff = decision_diff(x["original_text"], edited)
        views.append(
            {
                "created_at": x["created_at"],
                "action": x["action"],
                "note": x["note"] or "",
                "category": (x["category"] if "category" in keys else None) or "",
                "diff": diff,
            }
        )
    return views


@app.get("/", response_class=HTMLResponse)
def index(request: Request, conn: Conn):
    drafts = store.list_drafts(conn, store.STATUS_PENDING)
    return templates.TemplateResponse(
        request,
        "index.html",
        {"drafts": drafts, "status": "pending", "checks": _check_summaries(conn, drafts)},
    )


def _check_summaries(conn, drafts) -> dict[int, dict[str, int]]:
    """Per draft: how many claims are supported / contradicted / unverified / unchecked."""
    out: dict[int, dict[str, int]] = {}
    for d in drafts:
        n = len(d.draft.claims_to_verify)
        if not n:
            continue
        rows = verify_store.checks_for_draft(conn, d.id)
        counts = {"supported": 0, "contradicted": 0, "unverified": 0}
        for c in rows:
            counts[c.verdict] = counts.get(c.verdict, 0) + 1
        counts["unchecked"] = n - len(rows)
        out[d.id] = counts
    return out


@app.get("/status/{status}", response_class=HTMLResponse)
def by_status(status: str, request: Request, conn: Conn):
    if status not in store.STATUSES:
        raise HTTPException(404, "unknown status")
    drafts = store.list_drafts(conn, status)
    return templates.TemplateResponse(
        request,
        "index.html",
        {"drafts": drafts, "status": status, "checks": _check_summaries(conn, drafts)},
    )


@app.get("/drafts/{draft_id}", response_class=HTMLResponse)
def detail(draft_id: int, request: Request, conn: Conn):
    row = store.get_draft(conn, draft_id)
    if row is None:
        raise HTTPException(404, "no such draft")
    decisions = _decision_views(store.list_decisions(conn, draft_id))
    checks = {c.claim_index: c for c in verify_store.checks_for_draft(conn, draft_id)}
    return templates.TemplateResponse(
        request,
        "detail.html",
        {
            "d": row,
            "decisions": decisions,
            "thread_text": "\n---\n".join(row.draft.thread),
            "checks": checks,
            "contradicted": any(c.verdict == "contradicted" for c in checks.values()),
        },
    )


@app.get("/voice", response_class=HTMLResponse)
def voice(request: Request, conn: Conn, weeks: int | None = None):
    """Voice report (step 7) rendered from live drafts/decisions; nothing is changed."""
    cfg = load_draft_config()
    if weeks is None:
        weeks = int(cfg["report"].get("weeks", 4))
    if weeks < 1:
        raise HTTPException(400, "weeks must be >= 1")
    now = datetime.now(UTC)
    since = now - timedelta(weeks=weeks)
    report = build_report(
        store.fetch_draft_stats(conn, since),
        store.fetch_decisions_for_voice(conn, since),
        VOICE_PATH.read_text(encoding="utf-8"),
        cfg,
        now=now,
        weeks=weeks,
    )
    return templates.TemplateResponse(request, "voice.html", {"r": report, "weeks": weeks})


def _redirect_home() -> RedirectResponse:
    return RedirectResponse("/", status_code=303)


@app.post("/drafts/{draft_id}/approve")
async def approve(draft_id: int, request: Request, conn: Conn):
    form = await read_form(request)
    if verify_store.has_contradiction(conn, draft_id) and not form.get("override"):
        raise HTTPException(
            409, "a claim in this draft was contradicted by its source; edit it or approve anyway"
        )
    try:
        store.approve(conn, draft_id, note=_note(form))
    except KeyError as exc:
        raise HTTPException(404, "no such draft") from exc
    log.info("draft %d approved%s", draft_id, " (override)" if form.get("override") else "")
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
            category=_category(form),
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
        store.reject(conn, draft_id, note=_note(form), category=_category(form))
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
