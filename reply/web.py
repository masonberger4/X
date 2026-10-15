"""The reply page, included into the control panel (panel/app.py).

  GET  /replies                 paste screenshots of a post you found on X (the link of
                                your own post it answers, when it does, and a note on what
                                you want to say); recent requests below
  POST /replies                 save them and start writing the reply
  GET  /replies/{id}            the screenshots, what the writer read off them, and the
                                reply: copy it, edit it, write it again with a note
  GET  /replies/{id}/shot/{k}   one screenshot
  POST /replies/{id}/rewrite    write it again (with a note)
  POST /replies/{id}/posted     "I posted it": the reply as posted (edited or not) and its
                                link, logged for tracking
  POST /replies/{id}/link       the link of a reply logged without one
  POST /replies/{id}/discard    not replying after all

Screenshots arrive as data: URLs in an ordinary form post (the page's script reads a pasted
or chosen picture), so no multipart parser is needed. Writing runs in a thread of the app
(`spawn`), one Claude call per attempt (reply/writer.py); the page refreshes itself until
it is done. Nothing here posts: "I posted it" turns the reply into a draft (item_id
`reply:req<id>`, approved with the text as posted) and the panel's `confirm` hook logs it
through step 3's own module, exactly as the copy-paste page's "I posted it" does, so step
4's snapshots and report pick it up. A reply edited by hand is held to the same checks as
the writer's (reply/checks.py) before it is logged.
"""

from __future__ import annotations

import base64
import binascii
import logging
import re
import threading
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import parse_qs, quote

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from jinja2 import ChoiceLoader, FileSystemLoader

from approval_queue import store as queue_store
from draft.schema import MAX_POST_CHARS, SHAPE_SINGLE, Draft
from reply import checks, writer
from reply import store as S

log = logging.getLogger(__name__)

TEMPLATES_DIR = Path(__file__).with_name("templates")
router = APIRouter()
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

# Set by the panel: confirm(draft_id, url) logs an approved draft as posted by hand (False
# when it is not waiting any more); add_link(draft_id, url) gives a logged post its link and
# returns a message (or raises ValueError with the reason).
_hooks: dict[str, Callable[..., Any] | None] = {"confirm": None, "add_link": None}

_DATA_URL = re.compile(r"^data:(image/[a-z+.-]+);base64,(.+)$", re.S)
_TWEET_URL = re.compile(r"^https://(?:www\.)?(?:x|twitter)\.com/\w{1,15}/status/\d{5,25}\b")


def configure(
    *,
    queue_templates: Path | None,
    confirm: Callable[[int, str], bool] | None,
    add_link: Callable[[int, str], str] | None = None,
) -> None:
    """Wire the router into the panel: how to log a post, how to add its link, the layout."""
    _hooks["confirm"] = confirm
    _hooks["add_link"] = add_link
    loaders = [FileSystemLoader(str(TEMPLATES_DIR))]
    if queue_templates is not None:
        loaders.append(FileSystemLoader(str(queue_templates)))
    templates.env.loader = ChoiceLoader(loaders)


def _config() -> dict[str, Any]:
    from config import load_config

    return load_config()


def spawn(rid: int) -> None:
    """Write request `rid` in a thread of the app (tests replace this)."""
    threading.Thread(
        target=writer.run_request, args=(rid, _config()), name=f"reply-{rid}", daemon=True
    ).start()


def get_conn() -> Iterator[Any]:
    conn = S.connect()
    try:
        yield conn
    finally:
        conn.close()


Conn = Annotated[Any, Depends(get_conn)]


async def _form(request: Request) -> dict[str, list[str]]:
    body = (await request.body()).decode("utf-8", errors="replace")
    return parse_qs(body, keep_blank_values=True)


def _first(form: dict[str, list[str]], key: str) -> str:
    values = form.get(key) or [""]
    return values[-1].strip()


def parse_shots(values: list[str], max_shots: int, max_mb: float) -> list[tuple[str, bytes]]:
    """The pasted pictures as [(extension, bytes)]. Raises ValueError with the reason."""
    shots = []
    for v in values:
        v = v.strip()
        if not v:
            continue
        m = _DATA_URL.match(v)
        if not m or m.group(1) not in S.SHOT_TYPES:
            raise ValueError("a screenshot must be a PNG, JPEG, WebP or GIF picture")
        try:
            data = base64.b64decode(m.group(2), validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ValueError("a screenshot could not be read") from exc
        if len(data) > max_mb * 1024 * 1024:
            raise ValueError(f"a screenshot is over {max_mb:g} MB")
        shots.append((S.SHOT_TYPES[m.group(1)], data))
    if not shots:
        raise ValueError("paste or choose at least one screenshot of the post")
    if len(shots) > max_shots:
        raise ValueError(f"at most {max_shots} screenshots per reply")
    return shots


def _own_url(conn, value: str) -> str:
    """The link of the account's own post the screenshots answer, or "". Refused unless it
    is an x.com/<user>/status/<id> address of a post step 3 logged, so the writer can be
    given that post and its studio piece's fact base."""
    from publish.store import tweet_id_from_url

    value = value.strip()
    if not value:
        return ""
    if not _TWEET_URL.match(value):
        raise ValueError("your post's link must be an x.com/<user>/status/<id> address")
    if S.own_post(conn, tweet_id_from_url(value) or "") is None:
        raise ValueError(
            "no posted piece has that link: add it on the studio's performance page first "
            "(or leave the box empty)"
        )
    return value.split("?", 1)[0]


def _back(rid: int | None = None, *, error: str = "", flash: str = "") -> RedirectResponse:
    url = f"/replies/{rid}" if rid is not None else "/replies"
    if error:
        url += f"?error={quote(error)}"
    elif flash:
        url += f"?flash={quote(flash)}"
    return RedirectResponse(url, status_code=303)


def _settle(conn) -> None:
    s = writer.settings(_config())
    S.settle_dead(conn, s["timeout_seconds"] * s["max_attempts"] / 60 + 5)


@router.get("/replies", response_class=HTMLResponse)
def replies_index(request: Request, conn: Conn, error: str = "", flash: str = ""):
    _settle(conn)
    s = writer.settings(_config())
    return templates.TemplateResponse(
        request,
        "replies.html",
        {"requests": S.recent(conn), "error": error, "flash": flash, "s": s},
    )


@router.post("/replies")
async def replies_create(request: Request, conn: Conn):
    form = await _form(request)
    s = writer.settings(_config())
    try:
        shots = parse_shots(form.get("shot") or [], s["max_shots"], s["max_shot_mb"])
        own_url = _own_url(conn, _first(form, "own_url"))
    except ValueError as exc:
        return _back(error=str(exc))
    rid = S.create(conn, shots, own_url=own_url, note=_first(form, "note"))
    spawn(rid)
    log.info("reply request %d: writing", rid)
    return _back(rid)


def _require(conn, rid: int) -> S.Request:
    req = S.get(conn, rid)
    if req is None:
        raise HTTPException(404, "no such reply")
    return req


@router.get("/replies/{rid}", response_class=HTMLResponse)
def reply_page(request: Request, rid: int, conn: Conn, error: str = "", flash: str = ""):
    _settle(conn)
    req = _require(conn, rid)
    s = writer.settings(_config())
    from studio.xcount import x_length

    return templates.TemplateResponse(
        request,
        "reply.html",
        {
            "r": req,
            "error": error,
            "flash": flash,
            "s": s,
            "length": x_length(req.reply) if req.reply else 0,
            "linked": _linked(conn, req),
            "own": _own(conn, req),
        },
    )


def _own(conn, req: S.Request) -> S.OwnPost | None:
    """The account's own post this reply is about, for the page to name."""
    from publish.store import tweet_id_from_url

    if not req.own_url:
        return None
    return S.own_post(conn, tweet_id_from_url(req.own_url) or "")


def _linked(conn, req: S.Request) -> bool:
    """Whether a posted reply's log has its real X id (else "add the link" is offered)."""
    if req.draft_id is None:
        return False
    info = queue_store.publish_states(conn, [req.draft_id]).get(req.draft_id)
    return bool(info and info.tweet_url)


@router.get("/replies/{rid}/shot/{k}", include_in_schema=False)
def reply_shot(rid: int, k: int, conn: Conn):
    req = _require(conn, rid)
    paths = req.shot_paths()
    if not 0 <= k < len(paths) or not paths[k].is_file():
        raise HTTPException(404)
    return FileResponse(paths[k])


@router.post("/replies/{rid}/rewrite")
async def reply_rewrite(rid: int, request: Request, conn: Conn):
    form = await _form(request)
    _require(conn, rid)
    if not S.start_rewrite(conn, rid, _first(form, "note")):
        return _back(rid, error="this reply is being written, or is already posted or dropped")
    spawn(rid)
    return _back(rid)


@router.post("/replies/{rid}/discard")
async def reply_discard(rid: int, conn: Conn):
    req = _require(conn, rid)
    if req.status == S.POSTED:
        return _back(rid, error="this reply is already logged as posted")
    S.mark(conn, rid, S.DISCARDED)
    return _back(flash=f"reply {rid} dropped")


def _reply_to(req: S.Request) -> dict[str, Any]:
    return {
        "url": req.post_url,
        "author": req.author,
        "text": req.post_text,
        "request_id": req.id,
    }


@router.post("/replies/{rid}/posted")
async def reply_posted(rid: int, request: Request, conn: Conn):
    """Log the reply as posted: the text as it went out (checked again), and its link."""
    form = await _form(request)
    req = _require(conn, rid)
    if req.status not in (S.READY, S.BLOCKED):
        return _back(rid, error="there is no written reply waiting here to log")
    text = _first(form, "text").replace("\r\n", "\n").strip()
    url = _first(form, "url")
    s = writer.settings(_config())
    probs = checks.problems(
        text, allowed=checks.known_handles(_config(), req.author), max_chars=MAX_POST_CHARS
    )
    if probs:
        return _back(rid, error="Not logged: " + "; ".join(probs))
    if url and not _TWEET_URL.match(url):
        return _back(rid, error="the link must be your reply's x.com/<you>/status/<id> address")
    confirm = _hooks["confirm"]
    if confirm is None:
        return _back(rid, error="logging a post needs the control panel (run_app.py)")
    item_id = queue_store.reply_item_id(f"req{rid}")
    found = queue_store.find_by_item(conn, item_id)
    if found is None:
        draft = Draft(
            thread=[req.reply or text],
            suggested_visual="",
            why_it_matters=req.why,
            shape=SHAPE_SINGLE,
            anchors=[],
            max_chars=MAX_POST_CHARS,
            wanted_visuals=0,
            reply_to=_reply_to(req),
        )
        draft_id = queue_store.insert_draft(
            conn, item_id=item_id, model=req.model or s["model"], draft=draft
        )
    else:
        draft_id = found.id
    current = queue_store.get_draft(conn, draft_id)
    if current is not None and current.status == queue_store.STATUS_PENDING:
        if current.draft.thread != [text]:
            queue_store.edit(conn, draft_id, thread=[text], note="edited before posting")
        else:
            queue_store.approve(conn, draft_id, note="posted from the reply page")
    if not confirm(draft_id, url):
        return _back(rid, error="not logged: step 3 already has this reply (logged before?)")
    S.set_reply_text(conn, rid, text)
    S.mark(conn, rid, S.POSTED, draft_id=draft_id)
    log.info("reply request %d logged as posted (draft %d)", rid, draft_id)
    return _back(rid, flash="logged as posted")


@router.post("/replies/{rid}/link")
async def reply_link(rid: int, request: Request, conn: Conn):
    form = await _form(request)
    req = _require(conn, rid)
    add_link = _hooks["add_link"]
    if req.status != S.POSTED or req.draft_id is None or add_link is None:
        return _back(rid, error="only a reply logged as posted takes a link")
    try:
        msg = add_link(req.draft_id, _first(form, "url"))
    except ValueError as exc:
        return _back(rid, error=str(exc))
    return _back(rid, flash=msg)
