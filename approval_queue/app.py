"""Server-rendered approval queue: FastAPI + Jinja2, no JavaScript framework.

Routes:
  GET  /queue                 pending drafts (source, score, rationale); / redirects here
  GET  /drafts/{id}           detail: single_post, thread, claims_to_verify (+ step 2b
                              verdicts with source links), edit form
  POST /drafts/{id}/approve   refused with 409 while a claim is contradicted, unless the
                              form carries override=1
  POST /drafts/{id}/edit      saves edited text (+ approves unless 'keep_pending' is set)
  POST /drafts/{id}/revise    the drafter rewrites the draft from the form's 'instructions'
                              and from every claim step 2b contradicted (or could not
                              verify); the draft stays pending, old claim checks are dropped
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
from urllib.parse import parse_qs, quote

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from approval_queue import images, store
from draft import drafter
from draft.chart import alt_text
from draft.examples import parse_decision_text
from draft.prompt import VOICE_PATH, ClaimProblem
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
# The shared nav (base.html) shows the control-panel links only when the queue is
# served as part of it (panel/app.py sets this True); run_queue.py serves the queue alone.
templates.env.globals["HAS_PANEL"] = False

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
        if (
            x["action"] in (store.ACTION_EDIT, store.ACTION_REVISE)
            and edited
            and edited != x["original_text"]
        ):
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


@app.get("/", include_in_schema=False)
def root() -> RedirectResponse:
    """Standalone (run_queue.py) entry point. The control panel serves its dashboard
    here instead and this route never matches there."""
    return _redirect_home()


@app.get("/queue", response_class=HTMLResponse)
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
def detail(draft_id: int, request: Request, conn: Conn, error: str = "", revised: int = 0):
    row = store.get_draft(conn, draft_id)
    if row is None:
        raise HTTPException(404, "no such draft")
    decisions = _decision_views(store.list_decisions(conn, draft_id))
    checks = {c.claim_index: c for c in verify_store.checks_for_draft(conn, draft_id)}
    has_image = store.resolve_image(row.image_path) is not None
    table_checks = {(k.row, k.col): k for k in verify_store.table_checks_for_draft(conn, draft_id)}
    return templates.TemplateResponse(
        request,
        "detail.html",
        {
            "d": row,
            "table_checks": table_checks,
            "table_unverified": row.draft.table is not None and not has_image,
            "image_url": f"/drafts/{draft_id}/image" if has_image else "",
            "image_alt": row.image_alt
            or (alt_text(row.draft.visual, row.url) if row.draft.visual else ""),
            "decisions": decisions,
            "thread_text": "\n---\n".join(row.draft.thread),
            "checks": checks,
            "contradicted": any(c.verdict == "contradicted" for c in checks.values()),
            "claim_problems": len(claim_problems(checks.values())),
            "error": error,
            "revised": bool(revised),
        },
    )


def claim_problems(checks) -> list[ClaimProblem]:
    """The step 2b verdicts a revision must fix: contradicted claims, plus unverified ones
    the model is told to soften or drop. Supported claims are left alone."""
    out: list[ClaimProblem] = []
    for c in checks:
        if c.verdict == "supported":
            continue
        out.append(
            ClaimProblem(
                claim=c.claim,
                verdict=c.verdict,
                note=c.note,
                quote=c.quote,
                source_url=c.source_url,
            )
        )
    return out


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
    return RedirectResponse("/queue", status_code=303)


@app.post("/drafts/{draft_id}/approve")
async def approve(draft_id: int, request: Request, conn: Conn):
    form = await read_form(request)
    if verify_store.has_contradiction(conn, draft_id) and not form.get("override"):
        raise HTTPException(
            409, "a claim in this draft was contradicted by its source; edit it or approve anyway"
        )
    row = store.get_draft(conn, draft_id)
    if row is None:
        raise HTTPException(404, "no such draft")
    if row.draft.table is not None and store.resolve_image(row.image_path) is None:
        # The table's cells were never all verified, so its picture must never be attached
        # after the human has stopped looking: the post goes out text-only, on the record.
        store.drop_table(conn, draft_id, "not verified when the draft was approved")
        log.info("draft %d: unverified table dropped at approval", draft_id)
    store.approve(conn, draft_id, note=_note(form))
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


@app.post("/drafts/{draft_id}/revise")
async def revise(draft_id: int, request: Request, conn: Conn):
    """Send the draft back through the drafter with the human's instructions. Claims that
    step 2b contradicted (or could not verify) are always included, so a draft can be
    revised with an empty instruction box just to fix its fact-check failures. On success
    the draft is replaced, stays pending, and its claim checks are dropped, except a
    supported verdict whose claim text is unchanged, which is carried over so run_verify
    only checks the claims that are new or changed; on failure nothing changes and the
    detail page shows why."""
    form = await read_form(request)
    row = store.get_draft(conn, draft_id)
    if row is None:
        raise HTTPException(404, "no such draft")
    instructions = form.get("instructions", "").strip() or None
    problems = claim_problems(verify_store.checks_for_draft(conn, draft_id))
    if not instructions and not problems:
        return _detail_redirect(
            draft_id, error="say what should change, or run the claim check first"
        )
    category = _category(form)
    try:
        result = drafter.revise_item(
            current=row.draft,
            instructions=instructions,
            claim_problems=problems,
            title=row.title,
            abstract=row.abstract,
            url=row.url,
            source=row.source,
            suggested_angle=row.suggested_angle or None,
            rationale=row.rationale or None,
            # Looked up at call time so tests can monkeypatch draft.drafter.call_anthropic.
            call=drafter.call_anthropic,
        )
    except drafter.DraftRejected as exc:
        log.warning("draft %d: revision broke a hard rule: %s", draft_id, exc)
        return _detail_redirect(draft_id, error=f"the revision broke a hard rule: {exc}")
    except Exception as exc:  # API / network errors: keep the draft as it was
        log.error("draft %d: revision failed: %s", draft_id, exc)
        return _detail_redirect(draft_id, error=f"revision failed: {exc}")
    note = instructions
    if problems and not note:
        note = "fix fact-check failures"
    store.revise(
        conn, draft_id, draft=result.draft, model=result.model, note=note, category=category
    )
    kept = verify_store.carry_over_checks(
        conn, draft_id, [c.claim for c in result.draft.claims_to_verify]
    )
    kept_cells = verify_store.carry_over_table_checks(
        conn, draft_id, row.draft.table, result.draft.table
    )
    images.attach_chart(conn, draft_id, result.draft.chart, source_url=row.url)
    log.info(
        "draft %d revised (%d attempt(s), %d claim problem(s), %d verdict(s) and %d table "
        "cell(s) kept)",
        draft_id,
        result.attempts,
        len(problems),
        kept,
        kept_cells,
    )
    return _detail_redirect(draft_id, revised=True)


def _detail_redirect(draft_id: int, *, error: str = "", revised: bool = False) -> RedirectResponse:
    url = f"/drafts/{draft_id}"
    if error:
        url += f"?error={quote(error)}"
    elif revised:
        url += "?revised=1"
    return RedirectResponse(url, status_code=303)


@app.get("/drafts/{draft_id}/image", include_in_schema=False)
def image(draft_id: int, conn: Conn):
    """The rendered chart PNG, exactly the file run_publish.py would attach."""
    row = store.get_draft(conn, draft_id)
    if row is None:
        raise HTTPException(404, "no such draft")
    path = store.resolve_image(row.image_path)
    if path is None:
        raise HTTPException(404, "this draft has no image")
    return FileResponse(str(path), media_type="image/png")


@app.post("/drafts/{draft_id}/image/drop")
async def drop_image(draft_id: int, request: Request, conn: Conn):
    """Post the text without the chart: forgets the spec, deletes the PNG, logs a decision."""
    form = await read_form(request)
    try:
        store.drop_image(conn, draft_id, note=_note(form))
    except KeyError as exc:
        raise HTTPException(404, "no such draft") from exc
    log.info("draft %d: image dropped by the reviewer", draft_id)
    return _detail_redirect(draft_id)


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
