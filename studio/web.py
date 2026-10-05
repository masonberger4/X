"""The studio's pages, included into the control panel (panel/app.py).

  GET  /studio                 pieces, queued topics, "Write a piece" and the run buttons
  POST /studio/topics          queue a topic (words, a feed story, an angle) and start it
  POST /studio/topics/{id}/drop  drop a queued topic that has not started
  GET  /studio/{id}            one piece: stage, runs, fact base, posts, cards, fact-check
                               log, the session's live log, and what the editor can do next
  GET  /studio/{id}/card/{n}   a card picture (cards/card_<n>.png in the piece's folder)
  POST /studio/{id}/continue   after the research checkpoint: write the piece (with notes)
  POST /studio/{id}/revise     a finished piece: rewrite it with the editor's notes
  POST /studio/{id}/resume     a failed or interrupted piece: pick it up where it stopped
  POST /studio/{id}/discard    give up on a piece (its files stay on disk)
  GET  /studio/playbook        the playbook every session reads
  POST /studio/playbook        save the editor's edit of it (the data folder's copy)

Every button records what the editor wants in studio_pieces / studio_topics (this module
writes only the studio's own tables, through studio/store.py) and then asks the panel to
start the `studio_now` or `studio_resume` step from ops/config.yaml; the run itself, its
log and its Stop button are the runs page's, like every other step. Without a panel the
request waits for the next automatic studio run.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import parse_qs

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from jinja2 import ChoiceLoader, FileSystemLoader

from studio import angles as A
from studio import prompt as P
from studio import store as S
from studio.settings import DEFAULT_PLAYBOOK, PLAYBOOK_NAME, load_studio_config, playbook_path

log = logging.getLogger(__name__)

TEMPLATES_DIR = Path(__file__).with_name("templates")
STEP_NEW = "studio_now"
STEP_RESUME = "studio_resume"
LOG_TAIL_LINES = 400

router = APIRouter()
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

# Set by the panel when it includes this router: start_steps(["studio_now"]) starts a run
# of those configured steps and returns a message (or raises with the reason it cannot).
_starter: dict[str, Callable[[list[str]], str] | None] = {"start": None}


def configure(*, start_steps: Callable[[list[str]], str] | None, queue_templates: Path | None) -> None:
    """Wire the router into the panel: how to start a run, and the shared layout."""
    _starter["start"] = start_steps
    loaders = [FileSystemLoader(str(TEMPLATES_DIR))]
    if queue_templates is not None:
        loaders.append(FileSystemLoader(str(queue_templates)))
    templates.env.loader = ChoiceLoader(loaders)


def _data_folder() -> Path:
    from approval_queue import store as queue_store

    return queue_store.db_path().resolve().parent


def get_conn() -> Iterator[Any]:
    conn = S.connect(_db_path())
    try:
        yield conn
    finally:
        conn.close()


def _db_path() -> Path:
    from approval_queue import store as queue_store

    return queue_store.db_path()


Conn = Annotated[Any, Depends(get_conn)]


async def _form(request: Request) -> dict[str, list[str]]:
    body = (await request.body()).decode("utf-8", errors="replace")
    return parse_qs(body, keep_blank_values=True)


def _first(form: dict[str, list[str]], key: str) -> str:
    values = form.get(key) or [""]
    return values[0].strip()


def _start(steps: list[str]) -> str:
    start = _starter["start"]
    if start is None:
        return "saved; the next automatic studio run picks it up"
    try:
        return start(steps)
    except Exception as exc:  # a run of the step is already going, or ops refuses it
        return f"saved; it starts with the next studio run ({exc})"


def _redirect(path: str, flash: str = "") -> RedirectResponse:
    from urllib.parse import quote

    sep = "&" if "?" in path else "?"
    return RedirectResponse(f"{path}{sep}flash={quote(flash)}" if flash else path, status_code=303)


def _piece_or_404(conn: Any, piece_id: int) -> S.Piece:
    piece = S.get_piece(conn, piece_id)
    if piece is None:
        raise HTTPException(404, "no such piece")
    return piece


def _read(path: Path, limit: int = 400_000) -> str:
    try:
        return path.read_text(encoding="utf-8-sig", errors="replace")[:limit]
    except OSError:
        return ""


def _tail(path: Path, lines: int = LOG_TAIL_LINES) -> str:
    text = _read(path, limit=4_000_000)
    return "\n".join(text.splitlines()[-lines:])


def _posts(workspace: Path) -> list[str]:
    try:
        data = json.loads(_read(workspace / P.PIECE_FILE) or "{}")
    except ValueError:
        data = {}
    names = data.get("posts") if isinstance(data, dict) else None
    files = [workspace / str(n) for n in names] if isinstance(names, list) else []
    if not files:
        files = sorted((workspace / P.POSTS_DIR).glob("*.txt"))
    return [t for t in (_read(f).strip() for f in files) if t]


def _cards(workspace: Path) -> list[dict[str, Any]]:
    out = []
    for png in sorted((workspace / P.CARDS_DIR).glob("card_*.png")):
        stem = png.stem.removeprefix("card_")
        if stem.isdigit():
            out.append({"n": int(stem), "name": png.name})
    return sorted(out, key=lambda c: c["n"])


@router.get("/studio", response_class=HTMLResponse)
def studio_index(request: Request, conn: Conn, flash: str = ""):
    cfg = load_studio_config()
    try:
        library = A.load_angles()
    except (OSError, ValueError):
        library = {}
    pieces = S.list_pieces(conn, 40)
    return templates.TemplateResponse(
        request,
        "studio_index.html",
        {
            "pieces": pieces,
            "topics": S.queued_topics(conn),
            "angles": library,
            "cfg": cfg,
            "flash": flash,
            "running": [p for p in pieces if p.stage in S.RUNNING_STAGES],
            "step_new": STEP_NEW,
            "step_resume": STEP_RESUME,
        },
    )


@router.post("/studio/topics")
async def studio_queue_topic(request: Request, conn: Conn):
    form = await _form(request)
    topic = _first(form, "topic")
    story = _first(form, "story")
    angle = _first(form, "angle")
    checkpoint = bool(_first(form, "checkpoint"))
    cluster_id = int(story) if story.isdigit() else None
    if not topic and cluster_id is None:
        return _redirect("/studio", "type a topic or pick a story first")
    if angle:
        try:
            if angle not in A.load_angles():
                return _redirect("/studio", f"unknown angle {angle}")
        except (OSError, ValueError):
            pass
    S.queue_topic(conn, topic=topic, cluster_id=cluster_id, angle=angle, checkpoint=checkpoint)
    return _redirect("/studio", _start([STEP_NEW]))


@router.post("/studio/topics/{topic_id}/drop")
def studio_drop_topic(topic_id: int, conn: Conn):
    S.drop_topic(conn, topic_id)
    return _redirect("/studio", "topic dropped")


@router.get("/studio/playbook", response_class=HTMLResponse)
def studio_playbook(request: Request, flash: str = ""):
    path = playbook_path(_data_folder())
    return templates.TemplateResponse(
        request,
        "studio_playbook.html",
        {
            "text": _read(path),
            "editable_copy": path.name == PLAYBOOK_NAME,
            "path": str(path),
            "flash": flash,
        },
    )


@router.post("/studio/playbook")
async def studio_save_playbook(request: Request):
    form = await _form(request)
    text = (form.get("text") or [""])[0].replace("\r\n", "\n")
    if not text.strip():
        return _redirect("/studio/playbook", "the playbook cannot be empty")
    target = _data_folder() / PLAYBOOK_NAME
    tmp = target.with_suffix(".tmp")
    tmp.write_text(text.rstrip() + "\n", encoding="utf-8")
    tmp.replace(target)
    return _redirect("/studio/playbook", "saved; the next session reads it")


@router.post("/studio/playbook/reset")
def studio_reset_playbook():
    target = _data_folder() / PLAYBOOK_NAME
    if target.is_file():
        target.unlink()
    return _redirect("/studio/playbook", f"back to the shipped seed ({DEFAULT_PLAYBOOK.name})")


@router.get("/studio/{piece_id}", response_class=HTMLResponse)
def studio_piece(request: Request, piece_id: int, conn: Conn, flash: str = ""):
    piece = _piece_or_404(conn, piece_id)
    ws = Path(piece.workspace)
    research = piece.meta.get("research") or {}
    return templates.TemplateResponse(
        request,
        "studio_piece.html",
        {
            "piece": piece,
            "runs": S.list_runs(conn, piece.id),
            "research": research if isinstance(research, dict) else {},
            "factbase": _read(ws / P.FACTBASE_FILE),
            "factcheck": _read(ws / P.FACTCHECK_FILE),
            "posts": _posts(ws),
            "cards": _cards(ws),
            "log": _tail(ws / "session.log"),
            "running": piece.stage in S.RUNNING_STAGES,
            "warnings": piece.meta.get("warnings") or [],
            "problems": piece.meta.get("problems") or [],
            "flash": flash,
            "S": S,
        },
    )


@router.get("/studio/{piece_id}/card/{n}")
def studio_card(piece_id: int, n: int, conn: Conn):
    piece = _piece_or_404(conn, piece_id)
    path = Path(piece.workspace) / P.CARDS_DIR / f"card_{int(n)}.png"
    if not path.is_file():
        raise HTTPException(404, "no such card")
    return FileResponse(path, media_type="image/png")


@router.post("/studio/{piece_id}/continue")
async def studio_continue(request: Request, piece_id: int, conn: Conn):
    piece = _piece_or_404(conn, piece_id)
    if piece.stage != S.STAGE_RESEARCH_READY:
        return _redirect(f"/studio/{piece_id}", f"the piece is {piece.stage}, not waiting")
    form = await _form(request)
    S.request(conn, piece_id, S.REQUEST_CONTINUE, _first(form, "note"))
    return _redirect(f"/studio/{piece_id}", _start([STEP_RESUME]))


@router.post("/studio/{piece_id}/revise")
async def studio_revise(request: Request, piece_id: int, conn: Conn):
    piece = _piece_or_404(conn, piece_id)
    form = await _form(request)
    note = _first(form, "note")
    if not note:
        return _redirect(f"/studio/{piece_id}", "say what to change")
    if piece.stage != S.STAGE_READY:
        return _redirect(f"/studio/{piece_id}", f"the piece is {piece.stage}; only a finished piece is revised")
    if not _draft_revisable(piece.draft_id):
        return _redirect(
            f"/studio/{piece_id}",
            "its draft is no longer pending in the queue (approved, rejected or posted); "
            "reopen it there first",
        )
    S.request(conn, piece_id, S.REQUEST_REVISE, note)
    return _redirect(f"/studio/{piece_id}", _start([STEP_RESUME]))


@router.post("/studio/{piece_id}/resume")
async def studio_resume(request: Request, piece_id: int, conn: Conn):
    piece = _piece_or_404(conn, piece_id)
    if piece.stage not in (S.STAGE_FAILED, S.STAGE_INTERRUPTED):
        return _redirect(f"/studio/{piece_id}", f"the piece is {piece.stage}; nothing to resume")
    form = await _form(request)
    S.request(conn, piece_id, S.REQUEST_CONTINUE, _first(form, "note"))
    return _redirect(f"/studio/{piece_id}", _start([STEP_RESUME]))


@router.post("/studio/{piece_id}/discard")
def studio_discard(piece_id: int, conn: Conn):
    piece = _piece_or_404(conn, piece_id)
    if piece.stage in S.RUNNING_STAGES:
        return _redirect(f"/studio/{piece_id}", "stop the run on the runs page first")
    S.update_piece(conn, piece_id, stage=S.STAGE_DISCARDED, request="", request_note="")
    return _redirect("/studio", f"piece {piece_id} discarded (its files stay in {piece.workspace})")


def _draft_revisable(draft_id: int | None) -> bool:
    if draft_id is None:
        return False
    from approval_queue import store as queue_store

    qconn = queue_store.connect()
    try:
        row = queue_store.get_draft(qconn, draft_id)
    finally:
        qconn.close()
    return row is not None and row.status == queue_store.STATUS_PENDING
