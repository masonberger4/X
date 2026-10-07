"""Server-rendered approval queue: FastAPI + Jinja2, no JavaScript framework.

Routes:
  GET  /queue                 pending drafts (source, score, rationale); / redirects here
  GET  /drafts/{id}           detail: posts, pictures, edit form
  POST /drafts/{id}/approve
  POST /drafts/{id}/edit      saves edited text (+ approves unless 'keep_pending' is set)
  POST /drafts/{id}/reject
  POST /drafts/{id}/reopen    sends an approved draft back to pending, unless step 3 has
                              already claimed or posted it
  POST /drafts/{id}/release   drops the dead schedule row of an approved draft whose
                              publish attempt posted nothing (failed, refused, or a claim
                              left behind by a run that died), so the next run tries it
                              again; the draft stays approved
  GET  /status/{status}       approved / rejected / failed lists

The edit and reject forms take an optional category (why the draft was edited or
rejected); the detail page shows a before/after diff for every edit. A studio piece is
revised from its studio page, which resumes the session that wrote it.

A studio draft (step 10) is held while the studio works on its piece or a run of it waits
(store.studio_hold): approve, edit, reject and the picture drops answer 409 with the reason,
since the revision replaces the draft's text and cards when it lands.

Form bodies are parsed with urllib so no multipart dependency is needed.

There is no login, so a request that changes anything must come from the app's own pages:
`SameOriginOnly` answers 403 to a POST whose Origin (or Referer, when it has no Origin)
names another host than the one it was sent to, so a web page open in the same browser
cannot press the buttons. The control panel installs the same check on its app.
"""

from __future__ import annotations

import difflib
import logging
from collections.abc import Iterator, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated
from urllib.parse import parse_qs, quote, urlsplit

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import Headers
from starlette.types import ASGIApp, Receive, Scope, Send

import timeutil
from approval_queue import publishing, store
from draft.chart import alt_text
from draft.schema import MAX_POST_CHARS, tweet_length

log = logging.getLogger(__name__)

TEMPLATES_DIR = Path(__file__).with_name("templates")
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
templates.env.filters["tweet_length"] = tweet_length
# `|localtime` / `|localdate` render a stored UTC timestamp in the display zone from
# the root config.yaml. Storage stays UTC; only what the reviewer reads is converted.
timeutil.install_jinja_filters(templates.env)
templates.env.globals["MAX_POST_CHARS"] = MAX_POST_CHARS
templates.env.globals["DECISION_CATEGORIES"] = store.DECISION_CATEGORIES


def install_standalone_globals() -> None:
    """The template globals run_queue.py serves the queue with. The shared nav (base.html)
    shows the control-panel links only when the panel hosts the queue, and alone the queue
    shows no run buttons, so nothing is ever in flight and nothing can post. panel/app.py
    overwrites all three on this same environment when it adopts the queue's routes, which
    is process-wide; a test that wants the standalone pages calls this to put them back."""
    templates.env.globals["HAS_PANEL"] = False
    templates.env.globals["current_run"] = lambda: None
    templates.env.globals["current_runs"] = lambda: []
    templates.env.globals["busy_steps"] = lambda: set()
    templates.env.globals["publish_live"] = lambda: False
    templates.env.globals["publish_running"] = lambda: False


install_standalone_globals()

# Methods that change nothing on these pages; any other one must come from the app itself.
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


def _host_of(url: str) -> str:
    """The host[:port] a URL names, lower case, without a user part ("" for none)."""
    try:
        netloc = urlsplit(url).netloc
    except ValueError:
        return ""
    return netloc.rpartition("@")[2].lower()


def cross_site(method: str, headers: Mapping[str, str]) -> str:
    """Why a request must be refused as sent from another site ("" when it may go on).
    `headers` are looked up by lower-case name, as starlette's Headers are.

    The pages have no login, so without this any web page open in the operator's browser
    could submit a form to them: queue a studio topic and start an Opus session, rewrite the
    studio's playbook, start a run, approve or edit a draft. A browser says which page a
    form was sent from in Origin (in Referer when it sends no Origin), and a request that
    changes anything must come from the host it was sent to (Host): the app's own pages,
    whatever name or address the browser reached it by (localhost, the LAN, Tailscale).
    Origin "null" (a sandboxed frame, a data: URL) names no host and is refused. A request
    with neither header (curl, the tests, a script on this machine) goes on: a browser
    sends Origin with every cross-site POST."""
    if method.upper() in SAFE_METHODS:
        return ""
    for name in ("origin", "referer"):
        sender = (headers.get(name) or "").strip()
        if not sender:
            continue
        own = (headers.get("host") or "").strip().lower()
        if own and _host_of(sender) == own:
            return ""
        return (
            f"Refused: this request was sent from another site ({name.title()}: {sender[:200]}), "
            f"not from this app's own pages at {own or '(no host)'}. Nothing was changed."
        )
    return ""


class SameOriginOnly:
    """ASGI middleware: answers 403 to a request `cross_site` refuses, before any route
    runs. This app installs it, and so does the control panel, whose own app serves these
    routes and the studio's (panel/app.py)."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            why = cross_site(scope["method"], Headers(scope=scope))
            if why:
                log.warning("%s %s: %s", scope["method"], scope.get("path", ""), why)
                await PlainTextResponse(why, status_code=403)(scope, receive, send)
                return
        await self.app(scope, receive, send)


app = FastAPI(title="Approval queue")
app.add_middleware(SameOriginOnly)


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
    """A decision text (JSON or bare post) as display lines, one per thread post."""
    return [f"post {i}: {p}" for i, p in enumerate(store.parse_decision_text(text), 1)]


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
def index(request: Request, conn: Conn, notice: str = ""):
    drafts = store.list_drafts(conn, store.STATUS_PENDING)
    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "drafts": drafts,
            "status": "pending",
            "studio_holds": _studio_holds(conn, drafts),
            "publish": {},
            "hidden_posted": 0,
            "show_posted": False,
            "notice": notice,
        },
    )


def _studio_holds(conn, drafts) -> dict[int, str]:
    """Per studio draft the studio is working on: why it is held (store.studio_hold)."""
    holds = {d.id: store.studio_hold(conn, d) for d in drafts if d.studio_piece is not None}
    return {draft_id: why for draft_id, why in holds.items() if why}


@app.get("/status/{status}", response_class=HTMLResponse)
def by_status(status: str, request: Request, conn: Conn, posted: int = 0):
    """On the approved list each row carries what step 3 did with it (read-only), and drafts
    already posted are hidden unless ?posted=1: the approved page is the waiting list."""
    if status not in store.STATUSES:
        raise HTTPException(404, "unknown status")
    drafts = store.list_drafts(conn, status)
    publish = store.publish_states(conn, [d.id for d in drafts])
    hidden = 0
    if status == store.STATUS_APPROVED and not posted:
        shown = [
            d for d in drafts if publish.get(d.id, None) is None or publish[d.id].status != "posted"
        ]
        hidden = len(drafts) - len(shown)
        drafts = shown
    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "drafts": drafts,
            "status": status,
            "studio_holds": _studio_holds(conn, drafts),
            "publish": publish,
            "releasable": _releasable(conn, drafts, publish),
            "hidden_posted": hidden,
            "show_posted": bool(posted),
        },
    )


def _releasable(conn, drafts, publish: dict) -> dict[int, bool]:
    """Which of these drafts a human may put back in line (a dead publish attempt: failed,
    refused, or a stale claim). Only drafts step 3 is holding are asked about, so the usual
    page — every draft waiting with no schedule row, or a 'pending' one — costs nothing."""
    now = datetime.now(UTC)
    held = [d.id for d in drafts if publish.get(d.id) is not None]
    return {
        draft_id: not publishing.release_reason(conn, draft_id, publish[draft_id], now)
        for draft_id in held
    }


@app.get("/drafts/{draft_id}", response_class=HTMLResponse)
def detail(
    draft_id: int,
    request: Request,
    conn: Conn,
    error: str = "",
):
    return _render_detail(request, conn, draft_id, error=error)


def _render_detail(
    request: Request,
    conn,
    draft_id: int,
    *,
    error: str = "",
    edit_form: dict[str, str] | None = None,
    status_code: int = 200,
) -> HTMLResponse:
    """The detail page. A refused form post (text over 280 characters, a studio hold)
    renders this same page with the error at the top instead of FastAPI's bare JSON error,
    which has no way back; `edit_form` keeps what the human typed in the edit form."""
    row = store.get_draft(conn, draft_id)
    if row is None:
        raise HTTPException(404, "no such draft")
    decisions = _decision_views(store.list_decisions(conn, draft_id))
    image_file = store.resolve_image(row.image_path)
    has_image = image_file is not None
    publish_states = store.publish_states(conn, [draft_id])
    publish_info = publish_states.get(draft_id)
    edit_form = edit_form or {}
    return templates.TemplateResponse(
        request,
        "detail.html",
        {
            "d": row,
            "studio_hold": store.studio_hold(conn, row),
            "publish": publish_info,
            "releasable": _releasable(conn, [row], publish_states).get(draft_id, False),
            "image_url": f"/drafts/{draft_id}/image?v={_stamp(image_file)}" if has_image else "",
            "image_alt": row.image_alt
            or (alt_text(row.draft.visual, row.url) if row.draft.visual else ""),
            "extra_images": [
                {
                    "index": int(im["index"]),
                    "url": f"/drafts/{draft_id}/image/{int(im['index'])}"
                    f"?v={_stamp(store.resolve_image(im['path']))}",
                    "alt": im.get("alt", ""),
                    "anchor": im.get("anchor", 1),
                }
                for im in row.images
                if int(im.get("index", 0)) > 0 and store.resolve_image(im["path"]) is not None
            ],
            "first_anchor": (row.draft.anchors or [1])[0],
            "decisions": decisions,
            "thread_text": edit_form.get("thread", "\n---\n".join(row.draft.thread)),
            "edit_note": edit_form.get("note", ""),
            "edit_category": edit_form.get("category", ""),
            "edit_open": bool(edit_form),
            "error": error,
        },
        status_code=status_code,
    )


def _studio_held(request: Request, conn, draft_id: int) -> HTMLResponse | None:
    """The detail page with a 409 when the studio is working on this draft's piece (or a
    run of it is waiting): what the editor does here meanwhile would be overwritten by the
    revision, or make it fail after an hour's work. None when the draft is free."""
    row = store.get_draft(conn, draft_id)
    hold = store.studio_hold(conn, row) if row is not None else ""
    if not hold:
        return None
    return _render_detail(request, conn, draft_id, error=f"Not done: {hold}.", status_code=409)


def _redirect_home(*, notice: str = "") -> RedirectResponse:
    url = "/queue"
    if notice:
        url += f"?notice={quote(notice)}"
    return RedirectResponse(url, status_code=303)


@app.post("/drafts/{draft_id}/approve")
async def approve(draft_id: int, request: Request, conn: Conn):
    form = await read_form(request)
    held = _studio_held(request, conn, draft_id)
    if held is not None:
        return held
    if store.get_draft(conn, draft_id) is None:
        raise HTTPException(404, "no such draft")
    store.approve(conn, draft_id, note=_note(form))
    log.info("draft %d approved", draft_id)
    return _redirect_home()


def _edit_problem(thread: list[str], limit: int = MAX_POST_CHARS) -> str | None:
    """Why an edited text cannot be saved, naming the post and its length (URLs count as 23),
    or None when every post fits. `limit` is the draft's own per-post limit: 280, or a long
    post's (a format genome's, or the studio's X Premium limit)."""
    if not thread:
        return "the thread cannot be empty"
    over = []
    for label, text in [(f"post {i}", p) for i, p in enumerate(thread, 1)]:
        n = tweet_length(text)
        if n > limit:
            over.append(f"{label} is {n} characters")
    if over:
        return f"{len(over)} post(s) exceed {limit} characters ({'; '.join(over)})"
    return None


@app.post("/drafts/{draft_id}/edit")
async def edit(draft_id: int, request: Request, conn: Conn):
    form = await read_form(request)
    held = _studio_held(request, conn, draft_id)
    if held is not None:
        return held
    thread = _split_thread(form.get("thread", ""))
    current = store.get_draft(conn, draft_id)
    limit = max(MAX_POST_CHARS, current.draft.max_chars) if current is not None else MAX_POST_CHARS
    problem = _edit_problem(thread, limit)
    if problem is None:
        try:
            category = store.validate_category(form.get("category"))
        except ValueError as exc:
            problem = str(exc)
    if problem is not None:
        # Back to the same page, error on top and the typed text kept, never a JSON page.
        return _render_detail(
            request,
            conn,
            draft_id,
            error=f"Not saved: {problem}",
            edit_form={k: form.get(k, "") for k in ("thread", "note", "category")},
            status_code=400,
        )
    try:
        store.edit(
            conn,
            draft_id,
            thread=thread,
            note=_note(form),
            approve_after="keep_pending" not in form,
            category=category,
        )
    except KeyError as exc:
        raise HTTPException(404, "no such draft") from exc
    log.info("draft %d edited", draft_id)
    if "keep_pending" in form:
        return RedirectResponse(f"/drafts/{draft_id}", status_code=303)
    return _redirect_home()


def _detail_redirect(draft_id: int, *, error: str = "") -> RedirectResponse:
    url = f"/drafts/{draft_id}"
    if error:
        url += f"?error={quote(error)}"
    return RedirectResponse(url, status_code=303)


def _stamp(path: Path | None) -> int:
    """Modification time of a rendered picture, the `v=` in its URL: a revision writes
    the same file name, so without it a browser can show the previous picture."""
    try:
        return int(path.stat().st_mtime) if path is not None else 0
    except OSError:
        return 0


def _image_response(path: Path) -> FileResponse:
    """A draft's PNG. `no-cache` means revalidate, not "do not cache": the browser still
    gets a 304 from the ETag while the file is unchanged, and the new picture the moment a
    revision rewrites it."""
    return FileResponse(str(path), media_type="image/png", headers={"Cache-Control": "no-cache"})


@app.get("/drafts/{draft_id}/image", include_in_schema=False)
def image(draft_id: int, conn: Conn):
    """The first picture, exactly the file run_publish.py would attach."""
    row = store.get_draft(conn, draft_id)
    if row is None:
        raise HTTPException(404, "no such draft")
    path = store.resolve_image(row.image_path)
    if path is None:
        raise HTTPException(404, "this draft has no image")
    return _image_response(path)


@app.get("/drafts/{draft_id}/image/{index}", include_in_schema=False)
def image_at(draft_id: int, index: int, conn: Conn):
    """The k-th picture (index 0 is /drafts/{id}/image)."""
    row = store.get_draft(conn, draft_id)
    if row is None:
        raise HTTPException(404, "no such draft")
    for im in row.images:
        if int(im.get("index", 0)) == index:
            path = store.resolve_image(im["path"])
            if path is not None:
                return _image_response(path)
    raise HTTPException(404, "this draft has no such image")


@app.post("/drafts/{draft_id}/image/drop")
async def drop_image(draft_id: int, request: Request, conn: Conn):
    """Post the text without pictures: deletes every PNG, logs a decision."""
    form = await read_form(request)
    held = _studio_held(request, conn, draft_id)
    if held is not None:
        return held
    try:
        store.drop_image(conn, draft_id, note=_note(form))
    except KeyError as exc:
        raise HTTPException(404, "no such draft") from exc
    log.info("draft %d: image dropped by the reviewer", draft_id)
    return _detail_redirect(draft_id)


@app.post("/drafts/{draft_id}/image/{index}/drop")
async def drop_one_image(draft_id: int, index: int, request: Request, conn: Conn):
    """Drop just this picture, keeping the others; the ones after it move down a place."""
    form = await read_form(request)
    held = _studio_held(request, conn, draft_id)
    if held is not None:
        return held
    try:
        store.drop_image(conn, draft_id, note=_note(form), index=index)
    except KeyError as exc:
        raise HTTPException(404, "no such draft") from exc
    except IndexError as exc:
        raise HTTPException(404, str(exc)) from exc
    log.info("draft %d: image %d dropped by the reviewer", draft_id, index)
    return _detail_redirect(draft_id)


@app.post("/drafts/{draft_id}/reject")
async def reject(draft_id: int, request: Request, conn: Conn):
    form = await read_form(request)
    held = _studio_held(request, conn, draft_id)
    if held is not None:
        return held
    try:
        store.reject(conn, draft_id, note=_note(form), category=_category(form))
    except KeyError as exc:
        raise HTTPException(404, "no such draft") from exc
    log.info("draft %d rejected", draft_id)
    return _redirect_home()


@app.post("/drafts/{draft_id}/reopen")
async def reopen(draft_id: int, request: Request, conn: Conn):
    """Send an approved draft back to the pending queue so it can be edited or revised
    again. Refused once step 3 has claimed it (run_publish.py never re-reads the status
    after claiming) or already put it on X."""
    form = await read_form(request)

    def work() -> RedirectResponse:
        row = store.get_draft(conn, draft_id)
        if row is None:
            raise HTTPException(404, "no such draft")
        if row.status != store.STATUS_APPROVED:
            return _detail_redirect(
                draft_id, error=f"only an approved draft can be reopened (this one is {row.status})"
            )
        info = store.publish_states(conn, [draft_id]).get(draft_id)
        blocked = publishing.block_reason(conn, draft_id, info)
        if blocked:
            return _detail_redirect(draft_id, error=blocked)
        # The status goes first: a pending draft is invisible to fetch_approved, so no run
        # can claim it while we let go of its schedule row. The other order would leave the
        # row deleted and the draft still approved if this raised.
        try:
            store.reopen(conn, draft_id, note=_note(form))
        except KeyError as exc:
            raise HTTPException(404, "no such draft") from exc
        released = publishing.forget(conn, draft_id)
        log.info(
            "draft %d reopened for review (%d schedule row(s) released)", draft_id, len(released)
        )
        return RedirectResponse("/status/approved", status_code=303)

    return await run_in_threadpool(work)


@app.post("/drafts/{draft_id}/release")
async def release(draft_id: int, request: Request, conn: Conn):
    """Put an approved draft whose publish attempt posted nothing back in line, without
    taking it off the approved list: step 3's schedule row goes, so the next run considers
    it again. Refused for anything live on X and for a claim young enough that a run may
    still be posting it (`publishing.release_reason`, the same gate the button reads)."""
    await read_form(request)

    def work() -> RedirectResponse:
        row = store.get_draft(conn, draft_id)
        if row is None:
            raise HTTPException(404, "no such draft")
        if row.status != store.STATUS_APPROVED:
            return _detail_redirect(
                draft_id, error=f"only an approved draft can be released (this one is {row.status})"
            )
        now = datetime.now(UTC)
        info = store.publish_states(conn, [draft_id]).get(draft_id)
        blocked = publishing.release_reason(conn, draft_id, info, now)
        if blocked:
            return _detail_redirect(draft_id, error=blocked)
        released = publishing.release(conn, draft_id, info, now)
        if not released:
            return _detail_redirect(
                draft_id,
                error="publishing took this draft while the page was open; nothing released",
            )
        log.info(
            "draft %d released from a %s schedule row; the next publish run considers it again",
            draft_id,
            info.status if info else "?",
        )
        return RedirectResponse("/status/approved", status_code=303)

    return await run_in_threadpool(work)
