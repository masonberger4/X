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
  POST /studio/playbook        save the editor's edit of it as a new version
  POST /studio/playbook/reset  the shipped seed as a new version
  GET  /studio/performance     what X says: each posted piece's numbers, what each angle,
                               shape, hook style and card count has done, the lean, and
                               the playbook's history
  POST /studio/performance/{id}/metrics          a piece's numbers, typed in from X
  POST /studio/performance/{id}/link             the link of a piece posted by hand without it
  POST /studio/playbook/versions/{id}/revert     put an earlier version back (as a new one)
  POST /studio/playbook/versions/{id}/apply      apply the learning loop's open proposal
  GET  /studio/radar           where topics come from: the latest scan's topics, the feed's
                               top stories and the catalyst calendar, each with Write it
  POST /studio/radar/topics/{id}/write        queue a radar topic and start the studio
  POST /studio/radar/topics/{id}/dismiss      take it off the radar
  POST /studio/radar/catalysts/{id}/write     queue a preview (or a reaction) of a catalyst
  POST /studio/radar/catalysts/{id}/dismiss   take it off the calendar

Every button records what the editor wants in studio_pieces / studio_topics (this module
writes only the studio's own tables, through studio/store.py and studio/playbook.py) and
then asks the panel to start the `studio_now`, `studio_resume`, `studio_scan_now` or
`studio_learn_now` step from ops/config.yaml; the run itself, its log and its Stop button
are the runs page's, like every other step. Without a panel the request waits for the
next automatic studio run. The one write elsewhere is the performance page's "add the
link", which the panel does through step 3's own module (panel/publishing.py:add_head_link)
when it wires this router in.
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Callable, Iterator
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import parse_qs

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from jinja2 import ChoiceLoader, FileSystemLoader

from studio import angles as A
from studio import dashboard as D
from studio import evidence as E
from studio import ingest
from studio import learn as L
from studio import playbook as PB
from studio import prompt as P
from studio import radar as R
from studio import store as S
from studio import topics as T
from studio.settings import DEFAULT_PLAYBOOK, PLAYBOOK_NAME, load_studio_config, playbook_path

log = logging.getLogger(__name__)

TEMPLATES_DIR = Path(__file__).with_name("templates")
STEP_NEW = "studio_now"
STEP_RESUME = "studio_resume"
STEP_SCAN = "studio_scan_now"
SOON_DAYS = 30  # the calendar's "coming up" section; later ones are listed below it
STEP_LEARN = "studio_learn_now"
LOG_TAIL_LINES = 400
LOG_TAIL_BYTES = 4_000_000

router = APIRouter()
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

# Set by the panel when it includes this router: start_steps(["studio_now"]) starts a run
# of those configured steps and returns a message (or raises with the reason it cannot);
# add_link(draft_id, url) gives a draft posted by hand its X link and returns a message
# (or raises ValueError with the reason).
_starter: dict[str, Callable[..., str] | None] = {"start": None, "add_link": None}


def configure(
    *,
    start_steps: Callable[[list[str]], str] | None,
    queue_templates: Path | None,
    add_link: Callable[[int, str], str] | None = None,
) -> None:
    """Wire the router into the panel: how to start a run, how to add a post's link, and
    the shared layout."""
    _starter["start"] = start_steps
    _starter["add_link"] = add_link
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
    """303 to `path` with the message in its query (before any #fragment, which the
    browser keeps to itself)."""
    from urllib.parse import quote

    if not flash:
        return RedirectResponse(path, status_code=303)
    path, hash_, fragment = path.partition("#")
    sep = "&" if "?" in path else "?"
    return RedirectResponse(f"{path}{sep}flash={quote(flash)}{hash_}{fragment}", status_code=303)


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
    """The log's last `lines` lines, read from its last LOG_TAIL_BYTES (the newest end,
    however long a piece's log has grown)."""
    try:
        with open(path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            fh.seek(max(0, fh.tell() - LOG_TAIL_BYTES))
            data = fh.read()
    except OSError:
        return ""
    text = data.decode("utf-8-sig", errors="replace")
    return "\n".join(text.splitlines()[-lines:])


def _within(workspace: Path, rel: str) -> Path | None:
    """`rel` under the piece's folder, or None when it points outside it: piece.json is the
    session's writing, so the page reads only the files the checker (studio/qa.py) would."""
    try:
        path = (workspace / rel).resolve()
        path.relative_to(workspace.resolve())
    except (OSError, ValueError):
        return None
    return path


def _posts(workspace: Path) -> list[str]:
    try:
        data = json.loads(_read(workspace / P.PIECE_FILE) or "{}")
    except ValueError:
        data = {}
    names = data.get("posts") if isinstance(data, dict) else None
    named = [_within(workspace, str(n)) for n in names] if isinstance(names, list) else []
    files = [f for f in named if f is not None]
    if not files:
        files = sorted((workspace / P.POSTS_DIR).glob("*.txt"))
    return [t for t in (_read(f).strip() for f in files) if t]


def _research(value: Any) -> dict[str, Any]:
    """research.json as the piece page shows it. The session writes that file, so the
    candidate angles are the entries of a list of objects, whatever else the file holds:
    a number there would otherwise take the whole page down."""
    research = dict(value) if isinstance(value, dict) else {}
    angles = research.get("candidate_angles")
    research["candidate_angles"] = (
        [a for a in angles if isinstance(a, dict)] if isinstance(angles, list) else []
    )
    return research


def _cards(workspace: Path) -> list[dict[str, Any]]:
    """The drawn cards the piece uses: those piece.json lists (a revision that drops a card
    leaves its old picture in the folder), or every drawn card while there is no list."""
    try:
        data = json.loads(_read(workspace / P.PIECE_FILE) or "{}")
    except ValueError:
        data = {}
    listed = data.get("cards") if isinstance(data, dict) else None
    wanted: set[str] | None = None
    if isinstance(listed, list) and listed:
        wanted = set()
        for card in listed:
            rel = card.get("file") if isinstance(card, dict) else None
            path = _within(workspace, str(rel)) if rel else None
            if path is not None:
                wanted.add(path.with_suffix(".png").name)
    out = []
    for png in sorted((workspace / P.CARDS_DIR).glob("card_*.png")):
        stem = png.stem.removeprefix("card_")
        if stem.isdigit() and (wanted is None or png.name in wanted):
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
    # ASCII digits only: "²".isdigit() is true and int() refuses it.
    if story and not (story.isascii() and story.isdigit()):
        return _redirect("/studio", f"a story id is the number the feed shows, not {story!r}")
    cluster_id = int(story) if story else None
    if not topic and cluster_id is None:
        return _redirect("/studio", "type a topic or pick a story first")
    if angle:
        try:
            if angle not in A.load_angles():
                return _redirect("/studio", f"unknown angle {angle}")
        except (OSError, ValueError):
            pass
    S.queue_topic(
        conn,
        topic=topic,
        cluster_id=cluster_id,
        angle=angle,
        checkpoint=checkpoint,
        # one of the story's items, so the run still finds the story if linking merges it
        # into another cluster before the studio gets to it
        story_item=T.story_item(cluster_id) if cluster_id is not None else "",
    )
    return _redirect("/studio", _start([STEP_NEW]))


@router.post("/studio/topics/{topic_id}/drop")
def studio_drop_topic(topic_id: int, conn: Conn):
    S.drop_topic(conn, topic_id)
    return _redirect("/studio", "topic dropped")


def _seed_text() -> str:
    return _read(DEFAULT_PLAYBOOK)


@router.get("/studio/playbook", response_class=HTMLResponse)
def studio_playbook(request: Request, conn: Conn, flash: str = ""):
    path = playbook_path(_data_folder())
    text = _read(path)
    return templates.TemplateResponse(
        request,
        "studio_playbook.html",
        {
            "text": text,
            "editable_copy": path.name == PLAYBOOK_NAME,
            "is_seed": text == _seed_text(),
            "version": S.current_playbook_version(conn),
            "proposal": S.open_proposal(conn),
            "path": str(path),
            "flash": flash,
        },
    )


@router.post("/studio/playbook")
async def studio_save_playbook(request: Request, conn: Conn):
    form = await _form(request)
    text = (form.get("text") or [""])[0].replace("\r\n", "\n")
    if not text.strip():
        return _redirect("/studio/playbook", "the playbook cannot be empty")
    note = _first(form, "note")
    version = PB.save(
        conn,
        _data_folder(),
        text.rstrip() + "\n",
        source=S.PLAYBOOK_EDITOR,
        changelog=[note or "edited by hand"],
    )
    return _redirect("/studio/playbook", f"saved as version {version}; the next session reads it")


@router.post("/studio/playbook/reset")
def studio_reset_playbook(conn: Conn):
    seed = _seed_text()
    if not seed.strip():
        return _redirect("/studio/playbook", "the shipped seed is missing")
    version = PB.save(
        conn,
        _data_folder(),
        seed,
        source=S.PLAYBOOK_REVERT,
        changelog=[f"back to the shipped seed ({DEFAULT_PLAYBOOK.name})"],
    )
    return _redirect(
        "/studio/playbook",
        f"back to the shipped seed ({DEFAULT_PLAYBOOK.name}), as version {version}",
    )


@router.post("/studio/playbook/versions/{version_id}/revert")
def studio_revert_playbook(version_id: int, conn: Conn):
    try:
        new = PB.revert(conn, _data_folder(), version_id)
    except KeyError:
        raise HTTPException(404, "no such version") from None
    return _redirect(
        "/studio/performance#playbook",
        f"version {version_id} is back, as version {new}; the next session reads it",
    )


@router.post("/studio/playbook/versions/{version_id}/apply")
def studio_apply_proposal(version_id: int, conn: Conn):
    try:
        new = PB.apply_proposal(conn, _data_folder(), version_id)
    except KeyError:
        return _redirect(
            "/studio/performance#playbook",
            f"version {version_id} is not the open proposal any more (a later version overtook it)",
        )
    return _redirect(
        "/studio/performance#playbook",
        f"proposal {version_id} applied as version {new}; the next session reads it",
    )


@router.get("/studio/performance", response_class=HTMLResponse)
def studio_performance(request: Request, conn: Conn, flash: str = ""):
    from datetime import UTC, datetime

    cfg = load_studio_config()
    now = datetime.now(UTC)
    try:
        angle_keys = list(A.load_angles())
    except (OSError, ValueError):
        angle_keys = []
    ev, error = None, ""
    try:
        ev = E.measure(conn, cfg)
    except Exception as exc:  # a page that explains beats a 500
        log.exception("could not read what X says")
        error = f"could not read the numbers: {exc}"
    ev = ev or E.Evidence(measured=[], heads=[])
    last = S.last_learned(conn)
    return templates.TemplateResponse(
        request,
        "studio_performance.html",
        {
            "cfg": cfg,
            "kpi": L.describe_kpi(str(cfg["learn"]["kpi"])),
            "ev": ev,
            "rows": D.piece_rows(ev, cfg, now),
            "arms": D.arm_tables(ev, cfg, angles=angle_keys),
            "state": D.learn_state(ev, cfg, last, now),
            "versions": D.version_rows(S.playbook_versions(conn, 200), S.open_proposal(conn)),
            "said": E.block(ev, cfg),
            "metrics": S.MANUAL_METRICS,
            "small": len(ev.scored) < L.SMALL_SAMPLE,
            "error": error,
            "flash": flash,
            "can_link": _starter["add_link"] is not None,
            "step_learn": STEP_LEARN,
        },
    )


@router.post("/studio/performance/{piece_id}/metrics")
async def studio_add_metrics(request: Request, piece_id: int, conn: Conn):
    piece = _piece_or_404(conn, piece_id)
    form = await _form(request)
    counts: dict[str, int] = {}
    for name in S.MANUAL_METRICS:
        raw = _first(form, name).replace(",", "")
        if not raw:
            continue
        if not (raw.isascii() and raw.isdigit()):
            return _redirect("/studio/performance", f"{name}: a whole number, not {raw!r}")
        counts[name] = int(raw)
    if not counts:
        return _redirect("/studio/performance", "type at least one of the numbers X shows")
    S.add_manual_metrics(conn, piece.id, counts)
    return _redirect(
        "/studio/performance", f"numbers saved for piece {piece.id}; they count from now on"
    )


@router.post("/studio/performance/{piece_id}/link")
async def studio_add_link(request: Request, piece_id: int, conn: Conn):
    piece = _piece_or_404(conn, piece_id)
    add_link = _starter["add_link"]
    if add_link is None:
        return _redirect("/studio/performance", "adding a link needs the control panel")
    if piece.draft_id is None:
        return _redirect("/studio/performance", f"piece {piece.id} has no draft in the queue")
    form = await _form(request)
    try:
        message = add_link(piece.draft_id, _first(form, "url"))
    except ValueError as exc:
        return _redirect("/studio/performance", f"not linked: {exc}")
    return _redirect("/studio/performance", message)


def _today() -> date:
    from timeutil import display_tz

    return datetime.now(display_tz()).date()


def _angle_from(form: dict[str, list[str]]) -> str:
    """The angle the editor left selected, if the library still has it."""
    angle = _first(form, "angle")
    try:
        return angle if angle in A.load_angles() else ""
    except (OSError, ValueError):
        return ""


@router.get("/studio/radar", response_class=HTMLResponse)
def studio_radar(request: Request, conn: Conn, flash: str = ""):
    from studio import topics as T

    cfg = load_studio_config()
    rcfg = cfg["radar"]
    today = _today()
    try:
        library = A.load_angles()
    except (OSError, ValueError):
        library = {}
    since = datetime.now(UTC) - timedelta(days=float(rcfg["topic_days"]))
    topics = S.radar_topics(
        conn,
        since_iso=since.isoformat(timespec="seconds"),
        statuses=(S.RADAR_NEW, S.RADAR_QUEUED, S.RADAR_USED),
    )
    first = today - timedelta(days=int(rcfg["past_days"]))
    last = today + timedelta(days=int(rcfg["calendar_days"]))
    rows = S.catalysts_between(conn, first.isoformat(), last.isoformat())
    soon = (today + timedelta(days=SOON_DAYS)).isoformat()
    calendar = {
        "passed": [c for c in rows if c.date_end < today.isoformat()],
        "soon": [c for c in rows if c.date_end >= today.isoformat() and c.date_start <= soon],
        "later": [c for c in rows if c.date_start > soon],
    }
    suggested = {c.id: R.catalyst_text(c.to_catalyst(), today=today)[1] for c in rows}
    from studio.runner import taken_stories  # one story, one piece of writing

    feed = T.fetch_shortlist(
        {**cfg["topics"], "shortlist": int(rcfg["feed_stories"])}, exclude=taken_stories(conn)
    )
    return templates.TemplateResponse(
        request,
        "studio_radar.html",
        {
            "cfg": cfg,
            "scan": S.last_scan(conn),
            "done": S.last_scan(conn, S.SCAN_DONE),
            "topics": topics,
            "feed": feed,
            "calendar": calendar,
            "suggested": suggested,
            "angles": library,
            "kinds": R.KIND_LABELS,
            "today": today.isoformat(),
            "flash": flash,
            "step_scan": STEP_SCAN,
            "S": S,
        },
    )


def _radar_topic_or_404(conn: Any, radar_id: int) -> S.RadarTopic:
    topic = S.get_radar_topic(conn, radar_id)
    if topic is None:
        raise HTTPException(404, "no such radar topic")
    return topic


def _catalyst_or_404(conn: Any, catalyst_id: int) -> S.CatalystRow:
    row = S.get_catalyst(conn, catalyst_id)
    if row is None:
        raise HTTPException(404, "no such catalyst")
    return row


@router.post("/studio/radar/topics/{radar_id}/write")
async def studio_radar_write(request: Request, radar_id: int, conn: Conn):
    topic = _radar_topic_or_404(conn, radar_id)
    if topic.status != S.RADAR_NEW:
        return _redirect("/studio/radar", f"radar topic {radar_id} is {topic.status} already")
    form = await _form(request)
    text = R.topic_text(topic.to_topic(), scanned=topic.created_at[:10])
    topic_id = S.queue_topic(
        conn,
        topic=text,
        cluster_id=None,
        angle=_angle_from(form),
        checkpoint=bool(_first(form, "checkpoint")),
    )
    S.set_radar_topic(conn, radar_id, status=S.RADAR_QUEUED, topic_id=topic_id)
    return _redirect("/studio/radar", _start([STEP_NEW]))


@router.post("/studio/radar/topics/{radar_id}/dismiss")
def studio_radar_dismiss(radar_id: int, conn: Conn):
    topic = _radar_topic_or_404(conn, radar_id)
    if topic.status != S.RADAR_NEW:
        return _redirect("/studio/radar", f"radar topic {radar_id} is {topic.status}; it stays")
    S.set_radar_topic(conn, radar_id, status=S.RADAR_DISMISSED)
    return _redirect("/studio/radar", "topic dismissed")


@router.post("/studio/radar/catalysts/{catalyst_id}/write")
async def studio_catalyst_write(request: Request, catalyst_id: int, conn: Conn):
    row = _catalyst_or_404(conn, catalyst_id)
    if row.topic_id is not None or row.piece_id is not None:
        return _redirect(
            "/studio/radar", f"catalyst {catalyst_id} already has a piece queued or written"
        )
    form = await _form(request)
    text, _ = R.catalyst_text(row.to_catalyst(), today=_today())
    topic_id = S.queue_topic(
        conn,
        topic=text,
        cluster_id=None,
        angle=_angle_from(form),
        checkpoint=bool(_first(form, "checkpoint")),
    )
    S.set_catalyst(conn, catalyst_id, topic_id=topic_id)
    return _redirect("/studio/radar", _start([STEP_NEW]))


@router.post("/studio/radar/catalysts/{catalyst_id}/dismiss")
def studio_catalyst_dismiss(catalyst_id: int, conn: Conn):
    _catalyst_or_404(conn, catalyst_id)
    S.set_catalyst(conn, catalyst_id, status=S.CATALYST_DISMISSED)
    return _redirect("/studio/radar", "catalyst taken off the calendar")


def _queue_draft(piece: S.Piece) -> Any:
    """The piece's draft in the approval queue, or None."""
    from approval_queue import store as queue_store

    item_id = queue_store.studio_item_id(piece.id)
    return _queue(lambda qconn: queue_store.find_by_item(qconn, item_id))


@router.get("/studio/{piece_id}", response_class=HTMLResponse)
def studio_piece(request: Request, piece_id: int, conn: Conn, flash: str = ""):
    piece = _piece_or_404(conn, piece_id)
    ws = Path(piece.workspace)
    posts = _posts(ws)
    cards = _cards(ws)
    # What would be posted is the queue draft's text. It differs from the session's files
    # after a hand edit in the queue, or while a revision has not reached the queue yet.
    draft = _queue_draft(piece)
    queue_posts = list(draft.draft.thread) if draft is not None else []
    return templates.TemplateResponse(
        request,
        "studio_piece.html",
        {
            "piece": piece,
            "runs": S.list_runs(conn, piece.id),
            "research": _research(piece.meta.get("research")),
            "factbase": _read(ws / P.FACTBASE_FILE),
            "factcheck": _read(ws / P.FACTCHECK_FILE),
            "posts": posts,
            "cards": cards,
            "queue_draft": draft.id if draft is not None else None,
            "queue_posts": queue_posts if queue_posts != posts else [],
            "queue_pictures": len(draft.images) if draft is not None else None,
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
        return _redirect(
            f"/studio/{piece_id}", f"the piece is {piece.stage}; only a finished piece is revised"
        )
    blocked = _queue(lambda qconn: ingest.revise_blocker(qconn, piece))
    if blocked:
        return _redirect(f"/studio/{piece_id}", f"not revised: {blocked}")
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
    withdrawn = _queue(lambda qconn: ingest.withdraw(qconn, piece))
    message = f"piece {piece_id} discarded (its files stay in {piece.workspace})"
    if withdrawn is not None:
        message += f"; its pending draft {withdrawn} is rejected in the queue"
    return _redirect("/studio", message)


def _queue(work: Callable[[Any], Any]) -> Any:
    """Run one call against the approval queue's own connection (its tables, its rules)."""
    from approval_queue import store as queue_store

    qconn = queue_store.connect()
    try:
        return work(qconn)
    finally:
        qconn.close()
