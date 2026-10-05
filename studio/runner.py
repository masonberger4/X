"""What one `run_studio.py` invocation does: tidy up after a killed run, act on the
editor's requests, and decide whether to start a new piece (and on what)."""

from __future__ import annotations

import logging
import re
import sqlite3
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from ops import lock
from studio import angles as A
from studio import prompt as P
from studio import qa
from studio import render as render_mod
from studio import session as SS
from studio import store as S
from studio import topics as T
from studio.settings import (
    EXEMPLARS_DIR,
    load_studio_config,
    playbook_path,
    read_brief,
    workspace_root,
)

log = logging.getLogger(__name__)

LOCK_NAME = ".studio.lock"

_STAGE_OF = {
    S.STAGE_RESEARCHING: "research",
    S.STAGE_WRITING: "write",
    S.STAGE_POLISHING: "polish",
    S.STAGE_REVISING: "revise",
}


def _root_config() -> dict[str, Any]:
    from config import load_config

    return load_config()


def _db_conn() -> sqlite3.Connection:
    from approval_queue import store as queue_store

    conn = queue_store.connect()
    S.ensure_tables(conn)
    return conn


def _data_folder() -> Path:
    from approval_queue import store as queue_store

    return queue_store.db_path().resolve().parent


def references() -> list[str]:
    if not EXEMPLARS_DIR.is_dir():
        return []
    return sorted(p.name for p in EXEMPLARS_DIR.iterdir() if (p / "handoff.md").is_file())


def known_handles(root_cfg: dict[str, Any]) -> set[str]:
    from draft.tags import load_handles

    return {h.handle.lower() for h in load_handles(root_cfg)}


def system_text() -> str:
    return P.system_prompt(read_brief("session.md"), read_brief("voice.md"), read_brief("cards.md"))


def _slug(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return s[:40] or "piece"


def _opening(piece: S.Piece) -> str:
    f = Path(piece.workspace) / P.POSTS_DIR / "01.txt"
    try:
        text = f.read_text(encoding="utf-8-sig").strip()
    except OSError:
        return ""
    return text[:280]


def recent(conn: sqlite3.Connection, cfg: dict[str, Any]) -> list[S.Piece]:
    return S.recent_pieces(conn, int(cfg["variety"].get("recent_pieces_shown") or 8))


def build_brief(
    conn: sqlite3.Connection,
    cfg: dict[str, Any],
    piece: S.Piece,
    *,
    library: dict[str, A.Angle],
    playbook: str,
    today: str,
    tzname: str,
) -> P.Brief:
    pieces = [p for p in recent(conn, cfg) if p.id != piece.id]
    recent_pieces = [
        P.RecentPiece(
            date=p.created_at[:10],
            title=p.label,
            angle=p.angle,
            shape=p.shape,
            hook_style=p.hook_style,
            opening=_opening(p),
            companies=[
                str(c.get("name"))
                for c in (p.meta.get("research", {}) or {}).get("companies", [])
                if isinstance(c, dict) and c.get("name")
            ],
        )
        for p in pieces
    ]
    offer = A.offer(
        library,
        [p.angle for p in pieces],
        avoid=int(cfg["variety"].get("avoid_recent_angles") or 0),
        requested=piece.requested_angle,
    )
    hooks = A.hooks_to_avoid(
        [p.hook_style for p in pieces], int(cfg["variety"].get("avoid_recent_hooks") or 0)
    )
    story = T.fetch_story(piece.cluster_id) if piece.cluster_id is not None else None
    shortlist: list[P.Story] = []
    if story is None and not piece.topic and piece.stage == S.STAGE_RESEARCHING:
        # Only the research stage chooses a story; later stages have one.
        shortlist = T.fetch_shortlist(cfg["topics"], exclude=S.used_cluster_ids(conn))
    x = cfg["x"]
    return P.Brief(
        piece_id=piece.id,
        today=today,
        timezone=tzname,
        workspace=str(Path(piece.workspace).resolve()),
        reference_dir=str(EXEMPLARS_DIR.resolve()),
        references=references(),
        topic=piece.topic,
        story=story,
        shortlist=shortlist,
        offer=offer,
        hooks_to_avoid=hooks,
        recent=recent_pieces,
        playbook=playbook,
        long_post_max=int(x["long_post_max"]),
        thread_post_max=int(x["thread_post_max"]),
        short_post_max=int(x["short_post_max"]),
        max_cards=int(x["max_cards_total"]),
    )


def _today() -> tuple[str, str]:
    from timeutil import display_tz, timezone_name

    return datetime.now(display_tz()).strftime("%Y-%m-%d"), timezone_name()


def make_renderer(cfg: dict[str, Any]) -> qa.Renderer | None:
    try:
        browser = render_mod.find_browser(cfg["render"].get("browser"))
    except render_mod.RenderError as exc:
        log.warning("cards cannot be drawn: %s", exc)
        return None
    timeout = float(cfg["render"].get("timeout_seconds") or 60)

    def _render(html: Path, png: Path) -> render_mod.RenderResult:
        return render_mod.render_card(html, png, browser=browser, timeout=timeout)

    return _render


def make_context(
    conn: sqlite3.Connection, cfg: dict[str, Any], *, stop_after_research: bool = False
) -> SS.Context:
    from studio import ingest

    root_cfg = _root_config()
    library = A.load_angles()
    playbook = playbook_path(_data_folder()).read_text(encoding="utf-8")
    today, tzname = _today()

    def brief_for(piece: S.Piece) -> P.Brief:
        return build_brief(
            conn, cfg, piece, library=library, playbook=playbook, today=today, tzname=tzname
        )

    max_chars = max(int(cfg["x"]["long_post_max"]), int(cfg["x"]["thread_post_max"]))

    def do_ingest(piece: S.Piece, report: qa.Report) -> int:
        return ingest.to_queue(conn, piece, report, max_chars=max_chars)

    return SS.Context(
        conn=conn,
        cfg=cfg,
        root_cfg=root_cfg,
        system=system_text(),
        reference_dir=EXEMPLARS_DIR,
        known_handles=known_handles(root_cfg),
        brief_for=brief_for,
        renderer=make_renderer(cfg),
        ingest=do_ingest,
        echo=print,
        stop_after_research=stop_after_research,
    )


def mark_stale(conn: sqlite3.Connection) -> list[S.Piece]:
    """Pieces left mid-stage by a run that died. This process holds the studio lock, so
    no other run can be working on them."""
    stale = S.running_pieces(conn)
    for p in stale:
        stage = _STAGE_OF.get(p.stage, "write")
        S.close_open_runs(conn, p.id, "the run stopped before the stage finished")
        S.update_piece(
            conn,
            p.id,
            stage=S.STAGE_INTERRUPTED,
            error=f"the {stage} stage stopped before it finished (stopped, timed out or crashed)",
            meta={"failed_stage": stage},
        )
    return stale


def allowed_to_start(
    conn: sqlite3.Connection, cfg: dict[str, Any], now: datetime
) -> tuple[bool, str]:
    """May the automatic run start a new piece? (A button press skips this.)"""
    auto = cfg["auto"]
    if not auto.get("enabled"):
        return False, "automatic pieces are off (studio/config.yaml auto.enabled)"
    cap = int(auto.get("max_new_per_day") or 0)
    since = (now - timedelta(hours=24)).isoformat(timespec="seconds")
    made = S.pieces_since(conn, since, origin=S.ORIGIN_AUTO)
    if cap <= 0 or made >= cap:
        return False, f"{made} automatic piece(s) in the last 24 hours (limit {cap})"
    gap = float(auto.get("min_hours_between") or 0)
    last = S.last_started(conn)
    if last and gap > 0:
        last_dt = datetime.fromisoformat(last)
        if now - last_dt < timedelta(hours=gap):
            return False, f"the last piece started under {gap:g} hours ago"
    waiting = [p for p in S.list_pieces(conn, 20) if p.stage == S.STAGE_RESEARCH_READY]
    if waiting:
        return False, f"piece {waiting[0].id} is waiting at the research checkpoint"
    return True, ""


def new_piece(
    conn: sqlite3.Connection,
    cfg: dict[str, Any],
    *,
    origin: str,
    topic: str = "",
    cluster_id: int | None = None,
    angle: str = "",
    checkpoint: bool,
) -> S.Piece:
    if angle:
        library = A.load_angles()
        if angle not in library:
            raise ValueError(f"unknown angle {angle!r}; see studio/angles.yaml")
    session_id = str(uuid.uuid4())
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    name = f"{stamp}-{_slug(topic or (f'story-{cluster_id}' if cluster_id else 'auto'))}"
    folder = workspace_root(_data_folder(), cfg) / name
    folder.mkdir(parents=True, exist_ok=False)
    piece_id = S.create_piece(
        conn,
        origin=origin,
        topic=topic,
        cluster_id=cluster_id,
        requested_angle=angle,
        checkpoint=checkpoint,
        session_id=session_id,
        workspace=str(folder.resolve()),
        model=str(cfg["model"]),
        effort=str(cfg.get("effort") or ""),
    )
    piece = S.get_piece(conn, piece_id)
    assert piece is not None
    return piece


def act_on_requests(ctx: SS.Context) -> int:
    done = 0
    for piece in S.pending_requests(ctx.conn):
        what, note = piece.request, piece.request_note
        S.update_piece(ctx.conn, piece.id, request="", request_note="")
        log.info("piece %s: %s requested (stage %s)", piece.id, what, piece.stage)
        if what == S.REQUEST_CONTINUE and piece.stage == S.STAGE_RESEARCH_READY:
            out = SS.write(ctx, piece, note)
        elif what == S.REQUEST_CONTINUE and piece.stage in (S.STAGE_FAILED, S.STAGE_INTERRUPTED):
            out = SS.resume_interrupted(ctx, piece, note)
        elif what == S.REQUEST_REVISE and piece.stage == S.STAGE_READY:
            out = SS.revise(ctx, piece, note or "Improve the piece.")
        else:
            log.info("piece %s: nothing to do for %s at stage %s", piece.id, what, piece.stage)
            continue
        log.info("piece %s: %s (%s)", piece.id, out.stage, out.message)
        done += 1
    return done


def run(
    *,
    now: bool = False,
    resume_only: bool = False,
    topic: str = "",
    story: int | None = None,
    angle: str = "",
    checkpoint: bool | None = None,
    dry_run: bool = False,
) -> int:
    cfg = load_studio_config()
    root = workspace_root(_data_folder(), cfg)
    root.mkdir(parents=True, exist_ok=True)
    # One studio run at a time on this data folder, however it was started (the ops step,
    # a button, a terminal): a second run would resume the same sessions.
    held = lock.acquire(root / LOCK_NAME, trust_os_lock=True)
    if held is None:
        log.info("another studio run is in progress; nothing to do")
        return 0
    conn = _db_conn()
    try:
        stale = mark_stale(conn)
        for p in stale:
            log.warning("piece %s was interrupted mid-stage; resume it from the studio page", p.id)
        if dry_run:
            return _dry_run(conn, cfg, topic=topic, story=story, angle=angle)
        ctx = make_context(conn, cfg)
        act_on_requests(ctx)
        if resume_only:
            return 0
        queued = S.next_queued_topic(conn)
        if topic or story is not None:
            plan = dict(
                origin=S.ORIGIN_MANUAL,
                topic=topic,
                cluster_id=story,
                angle=angle,
                checkpoint=cfg["manual"]["checkpoint"] if checkpoint is None else checkpoint,
            )
        elif queued is not None:
            plan = dict(
                origin=S.ORIGIN_MANUAL,
                topic=queued.topic,
                cluster_id=queued.cluster_id,
                angle=queued.angle or angle,
                checkpoint=queued.checkpoint if checkpoint is None else checkpoint,
            )
        elif now:
            plan = dict(
                origin=S.ORIGIN_MANUAL,
                topic="",
                cluster_id=None,
                angle=angle,
                checkpoint=cfg["manual"]["checkpoint"] if checkpoint is None else checkpoint,
            )
        else:
            ok, why = allowed_to_start(conn, cfg, datetime.now(UTC))
            if not ok:
                log.info("no new piece: %s", why)
                return 0
            plan = dict(
                origin=S.ORIGIN_AUTO,
                topic="",
                cluster_id=None,
                angle="",
                checkpoint=bool(cfg["auto"]["checkpoint"]),
            )
        piece = new_piece(conn, cfg, **plan)
        if queued is not None and not (topic or story is not None):
            S.claim_topic(conn, queued.id, piece.id)
        log.info("piece %s started in %s", piece.id, piece.workspace)
        out = SS.research(ctx, piece)
        log.info("piece %s: %s (%s)", piece.id, out.stage, out.message)
        return 0 if out.stage in (S.STAGE_READY, S.STAGE_RESEARCH_READY) else 1
    finally:
        conn.close()
        held.release()


def _dry_run(
    conn: sqlite3.Connection, cfg: dict[str, Any], *, topic: str, story: int | None, angle: str
) -> int:
    library = A.load_angles()
    playbook = playbook_path(_data_folder()).read_text(encoding="utf-8")
    today, tzname = _today()
    fake = S.Piece(
        id=0,
        created_at="",
        updated_at="",
        origin="manual",
        topic=topic,
        cluster_id=story,
        requested_angle=angle,
        angle="",
        shape="",
        hook_style="",
        title="",
        stage=S.STAGE_RESEARCHING,
        checkpoint=True,
        session_id="(new)",
        workspace=str(workspace_root(_data_folder(), cfg) / "(new)"),
        model=str(cfg["model"]),
        effort=str(cfg.get("effort") or ""),
        draft_id=None,
        request="",
        request_note="",
        error="",
    )
    brief = build_brief(
        conn, cfg, fake, library=library, playbook=playbook, today=today, tzname=tzname
    )
    print(P.research_prompt(brief))
    return 0


def print_list() -> int:
    conn = _db_conn()
    try:
        for p in S.list_pieces(conn, 30):
            print(
                f"#{p.id:<4} {p.created_at[:16]}  {p.stage:<15} {p.angle or '-':<22} {p.label[:60]}"
                + (f"  [draft {p.draft_id}]" if p.draft_id else "")
                + (f"  !! {p.error[:80]}" if p.error else "")
            )
        for t in S.queued_topics(conn):
            print(f"queued topic {t.id}: {t.topic or f'story {t.cluster_id}'}")
    finally:
        conn.close()
    return 0
