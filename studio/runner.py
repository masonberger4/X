"""What one `run_studio.py` invocation does: tidy up after a killed run, act on the
editor's requests, and decide whether to start a new piece (and on what)."""

from __future__ import annotations

import logging
import re
import sqlite3
import uuid
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

from ops import lock
from studio import angles as A
from studio import prompt as P
from studio import qa
from studio import radar as R
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
SCAN_LOCK_NAME = ".studio_scan.lock"
RADAR_IN_BRIEF = 8  # radar topics an open piece is shown
COMING_UP_IN_BRIEF = 20  # catalysts an open piece is shown

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


def _companies(piece: S.Piece) -> list[str]:
    """The company names in a piece's research.json. The session writes that file, so a
    null or a string where the list belongs means no names, never a failed brief."""
    research = piece.meta.get("research")
    companies = research.get("companies") if isinstance(research, dict) else None
    if not isinstance(companies, list):
        return []
    return [str(c["name"]) for c in companies if isinstance(c, dict) and c.get("name")]


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
    from timeutil import fmt_date

    pieces = [p for p in recent(conn, cfg) if p.id != piece.id]
    recent_pieces = [
        P.RecentPiece(
            # The display zone's date, as `today` is: a UTC date can read as tomorrow.
            date=fmt_date(p.created_at),
            title=p.label,
            angle=p.angle,
            shape=p.shape,
            hook_style=p.hook_style,
            opening=_opening(p),
            companies=_companies(p),
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
    radar: list[tuple[int, R.Topic]] = []
    coming: list[R.Catalyst] = []
    if story is None and not piece.topic and piece.stage == S.STAGE_RESEARCHING:
        # Only the research stage chooses a story; later stages have one.
        shortlist = T.fetch_shortlist(cfg["topics"], exclude=S.used_cluster_ids(conn))
        radar, coming = radar_for_brief(conn, cfg, date.fromisoformat(today))
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
        radar=radar,
        coming_up=coming,
        offer=offer,
        hooks_to_avoid=hooks,
        recent=recent_pieces,
        playbook=playbook,
        # The limits the checker enforces (qa.check_text keeps `headroom` under X's own),
        # so a post written to the number it is given is never sent back as too long.
        long_post_max=int(x["long_post_max"]) - int(x.get("headroom") or 0),
        thread_post_max=int(x["thread_post_max"]) - int(x.get("headroom") or 0),
        short_post_max=int(x["short_post_max"]),
        max_cards=int(x["max_cards_total"]),
    )


def radar_for_brief(
    conn: sqlite3.Connection, cfg: dict[str, Any], today: date
) -> tuple[list[tuple[int, R.Topic]], list[R.Catalyst]]:
    """What an open piece is offered from the radar: the recent scans' topics nobody has
    taken, and the catalysts coming up or just passed. Nothing when the radar is off."""
    from studio import scan as SC

    rcfg = cfg["radar"]
    if not rcfg.get("enabled"):
        return [], []
    since = datetime.now(UTC) - timedelta(days=float(rcfg["topic_days"]))
    topics = S.radar_topics(conn, since_iso=since.isoformat(timespec="seconds"))
    coming = R.coming_up(
        SC.known_catalysts(conn, rcfg, today),
        today=today,
        ahead_days=int(rcfg["brief_upcoming_days"]),
        back_days=int(rcfg["brief_recent_days"]),
    )
    return [(t.id, t.to_topic()) for t in topics[:RADAR_IN_BRIEF]], coming[:COMING_UP_IN_BRIEF]


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
        # Asked by every revision, a resumed one too, before its session runs: a draft
        # approved or rejected meanwhile would refuse the result after an hour's work.
        revisable=lambda piece: ingest.revise_blocker(conn, piece),
        harvest=lambda piece, info: _harvest(conn, cfg, piece, info, today),
    )


def _harvest(
    conn: sqlite3.Connection, cfg: dict[str, Any], piece: S.Piece, info: dict[str, Any], today: str
) -> None:
    from studio import scan as SC

    note = SC.harvest(conn, cfg, piece, info, today=date.fromisoformat(today))
    if note:
        log.info("piece %s: %s", piece.id, note)


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
    waiting = S.first_in_stage(conn, S.STAGE_RESEARCH_READY)
    if waiting is not None:
        return False, f"piece {waiting.id} is waiting at the research checkpoint"
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
    root = workspace_root(_data_folder(), cfg)
    folder, n = root / name, 1
    while True:  # two pieces on the same topic in the same second get folders of their own
        try:
            folder.mkdir(parents=True, exist_ok=False)
            break
        except FileExistsError:
            n += 1
            folder = root / f"{name}-{n}"
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
    for listed in S.pending_requests(ctx.conn):
        # Read again: the request before this one may have run for an hour, and meanwhile the
        # editor may have discarded this piece (which drops its request) or changed the note.
        piece = S.get_piece(ctx.conn, listed.id)
        if piece is None or not piece.request:
            continue
        what, note = piece.request, piece.request_note
        S.update_piece(ctx.conn, piece.id, request="", request_note="")
        log.info("piece %s: %s requested (stage %s)", piece.id, what, piece.stage)
        if what == S.REQUEST_CONTINUE and piece.stage == S.STAGE_RESEARCH_READY:
            out = SS.write(ctx, piece, note)
        elif what == S.REQUEST_CONTINUE and piece.stage in (S.STAGE_FAILED, S.STAGE_INTERRUPTED):
            out = SS.resume_interrupted(ctx, piece, note)
        elif what == S.REQUEST_REVISE and piece.stage == S.STAGE_READY:
            from studio import ingest

            gone = ingest.revise_blocker(ctx.conn, piece)
            if gone:
                # The editor approved or rejected it after asking: the queue would refuse
                # the revised text, so no session is spent and the piece stays ready.
                log.warning("piece %s: revision not run: %s", piece.id, gone)
                S.update_piece(ctx.conn, piece.id, error=f"not revised: {gone}")
                continue
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
    if dry_run:  # read-only: no lock, no stale marking, the piece the next run would start
        conn = _db_conn()
        try:
            queued = None if (topic or story is not None) else S.next_queued_topic(conn)
            if queued is not None:
                topic, story = queued.topic, queued.cluster_id
                angle = queued.angle or angle
            return _dry_run(conn, cfg, topic=topic, story=story, angle=angle)
        finally:
            conn.close()
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
            if plan["angle"] and plan["angle"] not in A.load_angles():
                # Queued before the angle left studio/angles.yaml: the session chooses
                # instead, rather than the topic failing every run.
                log.warning(
                    "queued topic %s asked for angle %r, which is no longer in the library; "
                    "the session will choose",
                    queued.id,
                    plan["angle"],
                )
                plan["angle"] = ""
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
        if plan["cluster_id"] is not None and T.fetch_story(plan["cluster_id"]) is None:
            gone = f"story {plan['cluster_id']} is not in the feed any more (merged or removed)"
            if topic or story is not None or queued is None:
                log.error("%s; nothing started", gone)
                return 2
            if not plan["topic"]:
                log.warning("%s; queued topic %s dropped", gone, queued.id)
                S.drop_topic(conn, queued.id)
                return 0
            log.warning("%s; writing on the queued topic's words alone", gone)
            plan["cluster_id"] = None
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
    from timeutil import fmt_datetime

    conn = _db_conn()
    try:
        for p in S.list_pieces(conn, 30):
            print(
                f"#{p.id:<4} {fmt_datetime(p.created_at):<20}  {p.stage:<15} "
                f"{p.angle or '-':<22} {p.label[:60]}"
                + (f"  [draft {p.draft_id}]" if p.draft_id else "")
                + (f"  !! {p.error[:80]}" if p.error else "")
            )
        for t in S.queued_topics(conn):
            print(f"queued topic {t.id}: {t.topic or f'story {t.cluster_id}'}")
    finally:
        conn.close()
    return 0


# --- the radar's scan (run_studio.py --scan) ----------------------------------------------


def feed_lines(cfg: dict[str, Any], conn: sqlite3.Connection) -> list[str]:
    """The feed's top unused stories, one line each, as leads for the scan."""
    stories = T.fetch_shortlist(cfg["topics"], exclude=S.used_cluster_ids(conn))
    return [
        f"{s.title} ({', '.join(x for x in (s.source, s.published) if x)})"
        + (f" [feed score {s.score}/50]" if s.score is not None else "")
        for s in stories
    ]


def scan(*, dry_run: bool = False, force: bool = False) -> int:
    """The radar's scan: when the last one finished `radar.scan_every_hours` ago (or
    `force`), one call with web search proposes topics and reports catalysts. Starts no
    piece. `dry_run` prints the prompt and changes nothing."""
    from studio import scan as SC

    cfg = load_studio_config()
    rcfg = cfg["radar"]
    if not rcfg.get("enabled"):
        log.info("the radar is off (studio/config.yaml radar.enabled)")
        return 0
    conn = _db_conn()
    try:
        today_text, tzname = _today()
        today = date.fromisoformat(today_text)
        angles = {key: a.question for key, a in A.load_angles().items()}
        if dry_run:
            system, user = SC.prompt_for(
                conn, cfg, today=today, tzname=tzname, angles=angles, feed=feed_lines(cfg, conn)
            )
            print("--- the scan's system prompt ---\n" + system)
            print("\n--- and its user prompt ---\n" + user)
            return 0
        last = S.last_scan(conn, S.SCAN_DONE)
        if not force and not SC.scan_due(last, datetime.now(UTC), float(rcfg["scan_every_hours"])):
            log.info(
                "no scan: the last one (%s) is under %sh old",
                last.finished_at if last else "",
                rcfg["scan_every_hours"],
            )
            return 0
        root = workspace_root(_data_folder(), cfg)
        root.mkdir(parents=True, exist_ok=True)
        held = lock.acquire(root / SCAN_LOCK_NAME, trust_os_lock=True)
        if held is None:
            log.info("another scan is in progress; nothing to do")
            return 0
        try:
            S.close_running_scans(conn, "the run stopped before the scan finished")
            out = SC.run_scan(
                conn,
                cfg,
                today=today,
                tzname=tzname,
                angles=angles,
                feed=feed_lines(cfg, conn),
                root_cfg=_root_config(),
            )
        finally:
            held.release()
        (log.info if out.ok else log.error)("%s", out.message)
        return 0 if out.ok else 1
    finally:
        conn.close()
