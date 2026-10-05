"""Runs a piece through its stages: research, write, polish (and revise on request).

Each stage is one invocation of the Claude Code CLI on the SAME session (the first with
--session-id, the rest with --resume), from the piece's own folder, so the session keeps
everything it read and wrote. Between stages the app checks the work (studio/qa.py) and
records where the piece is in studio_pieces before every launch, so a run that is
killed (the Stop button, a timeout, a crash) leaves a piece that can be resumed.

The CLI launcher is a parameter (`launch`, default claude_cli.run_session) and so is the
card renderer, so tests drive the whole flow without a model or a browser.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import claude_cli
from studio import ingest, qa
from studio import prompt as P
from studio import store as S
from studio.settings import stage_max_turns, stage_timeout

log = logging.getLogger(__name__)

TRANSCRIPT = "session.ndjson"
LOG_FILE = "session.log"

Launcher = Callable[..., claude_cli.SessionResult]


@dataclass
class Context:
    """What a stage run needs besides the piece: settings, the system prompt, the brief
    builder and the plumbing."""

    conn: sqlite3.Connection
    cfg: dict[str, Any]
    root_cfg: dict[str, Any] | None
    system: str
    reference_dir: Path
    known_handles: set[str]
    brief_for: Callable[[S.Piece], P.Brief]
    renderer: qa.Renderer | None
    ingest: Callable[[S.Piece, qa.Report], int]
    launch: Launcher | None = None  # None: claude_cli.run_session, looked up at call time
    echo: Callable[[str], None] = print
    stop_after_research: bool = False
    # Why a revision could not go into the queue ("" when it can), asked before it runs;
    # None skips the check (studio/ingest.py:revise_blocker in the app).
    revisable: Callable[[S.Piece], str] | None = None
    # What research.json gives the radar once research is done (the radar topic it used,
    # the catalysts it found: studio/scan.py:harvest in the app); None skips it. It never
    # fails the piece.
    harvest: Callable[[S.Piece, dict[str, Any]], None] | None = None
    # The editor's changes to the piece's queue draft since the last ingest, or None
    # (studio/ingest.py:hand_edits in the app); None here never looks.
    queue_edits: Callable[[S.Piece], ingest.HandEdits | None] | None = None


@dataclass
class Outcome:
    stage: str
    message: str
    report: qa.Report | None = None
    extra: dict[str, Any] = field(default_factory=dict)


def _write_log(workspace: Path, line: str) -> None:
    try:
        with open(workspace / LOG_FILE, "a", encoding="utf-8") as fh:
            fh.write(line.rstrip() + "\n")
    except OSError:
        pass


def _run_stage(
    ctx: Context, piece: S.Piece, stage: str, prompt_text: str, *, first: bool
) -> claude_cli.SessionResult:
    """One CLI invocation. Records a studio_runs row around it. A CLI that cannot run at
    all leaves the piece interrupted (so it can be resumed at once) and the error goes up,
    so the step shows as failed."""
    workspace = Path(piece.workspace)
    run_id = S.start_run(ctx.conn, piece.id, stage)
    _write_log(workspace, f"=== {stage} ===")
    started = False

    def say(line: str) -> None:
        _write_log(workspace, line)
        ctx.echo(f"[piece {piece.id} {stage}] {line}")

    def on_event(event: dict[str, Any]) -> None:
        nonlocal started
        if event.get("type") == "result":
            return  # one "stage ended" line below, from the run's last result
        if event.get("type") == "system" and event.get("subtype") == "init":
            if started:
                return  # the CLI starts another turn when a background sub-agent ends
            started = True
        for line in claude_cli.describe_event(event):
            say(line)

    launch = ctx.launch or claude_cli.run_session
    try:
        result = launch(
            prompt_text,
            model=piece.model,
            cwd=workspace,
            session_id=piece.session_id,
            resume=not first,
            system=ctx.system if first else "",
            effort=piece.effort or None,
            tools=ctx.cfg["tools"],
            allowed=ctx.cfg["tools"],
            add_dirs=[str(ctx.reference_dir)],
            flags=ctx.cfg["cli_flags"],
            max_turns=stage_max_turns(ctx.cfg, stage),
            timeout=stage_timeout(ctx.cfg, stage),
            transcript=workspace / TRANSCRIPT,
            on_event=on_event,
            cfg=ctx.root_cfg,
        )
    except claude_cli.ClaudeCliError as exc:
        S.finish_run(ctx.conn, run_id, outcome="error", detail=str(exc))
        _fail(ctx, piece, stage, f"the CLI could not run: {exc}", interrupted=True)
        raise
    say(
        f"stage ended: {result.subtype or ('success' if result.ok else 'error')} after "
        f"{result.num_turns} turns"
    )
    S.finish_run(
        ctx.conn,
        run_id,
        outcome="ok" if result.ok else (result.subtype or "error"),
        detail=result.detail,
        turns=result.num_turns,
        cost_usd=result.cost_usd,
        duration_ms=result.duration_ms,
    )
    return result


def _fail(
    ctx: Context, piece: S.Piece, stage: str, message: str, *, interrupted: bool = False
) -> Outcome:
    new_stage = S.STAGE_INTERRUPTED if interrupted else S.STAGE_FAILED
    S.update_piece(ctx.conn, piece.id, stage=new_stage, error=message, meta={"failed_stage": stage})
    _write_log(Path(piece.workspace), f"!!! {stage}: {message}")
    return Outcome(stage=new_stage, message=message)


def _stopped_early(result: claude_cli.SessionResult) -> bool:
    """A stage that ran out of turns or time can be resumed rather than failed."""
    return result.subtype in ("error_max_turns", "killed", "no_result")


def research(
    ctx: Context,
    piece: S.Piece,
    *,
    first: bool = True,
    resumed_reason: str = "",
    note: str = "",
) -> Outcome:
    S.update_piece(ctx.conn, piece.id, stage=S.STAGE_RESEARCHING, error="")
    piece = S.get_piece(ctx.conn, piece.id) or piece
    brief = ctx.brief_for(piece)
    # The stories the session may name in research.json: every one offered so far (a
    # resumed research run may be offered a newer shortlist than the first).
    offered = sorted({*_offered(piece), *(s.cluster_id for s in brief.shortlist)})
    S.update_piece(ctx.conn, piece.id, meta={"offered_stories": offered})
    text = P.research_prompt(brief)
    if note.strip():  # the editor's words when pressing Resume
        text += f"\n\nTHE EDITOR ADDS\n{note.strip()}"
    if resumed_reason:
        text = P.resume_prompt("research", resumed_reason, text)
    result = _run_stage(ctx, piece, "research", text, first=first)
    if not result.ok:
        return _fail(ctx, piece, "research", result.detail, interrupted=_stopped_early(result))
    workspace = Path(piece.workspace)
    if not (workspace / P.FACTBASE_FILE).is_file():
        return _fail(
            ctx, piece, "research", f"the session finished without writing {P.FACTBASE_FILE}"
        )
    info = read_research(workspace)
    updates: dict[str, Any] = {"meta": {"research": info}}
    if info.get("topic") and not piece.topic:
        updates["title"] = str(info["topic"])[:200]
    story_id = _story_number(info.get("story_id"))
    if story_id is not None and piece.cluster_id is None:
        # Only a story the brief offered ties the piece to it: the drafter skips a story
        # that has a draft, the studio's shortlist skips one a piece used, and the weekly
        # report reads that story's scores for the post. An invented or mistyped number
        # would do all three to a story the piece is not about.
        if story_id in offered:
            updates["cluster_id"] = story_id
        else:
            log.warning(
                "piece %s: research.json names story %s, which was not offered", piece.id, story_id
            )
            _write_log(workspace, f"research.json names story {story_id}, not one offered; ignored")
    S.update_piece(ctx.conn, piece.id, **updates)
    piece = S.get_piece(ctx.conn, piece.id) or piece
    if ctx.harvest is not None:
        try:
            ctx.harvest(piece, info)
        except Exception:  # the calendar is a by-product: the piece goes on without it
            log.warning(
                "piece %s: could not take its catalysts for the radar", piece.id, exc_info=True
            )
    if piece.checkpoint or ctx.stop_after_research:
        S.update_piece(ctx.conn, piece.id, stage=S.STAGE_RESEARCH_READY)
        return Outcome(S.STAGE_RESEARCH_READY, "research done; waiting for the editor")
    return write(ctx, piece)


def _offered(piece: S.Piece) -> list[int]:
    ids = piece.meta.get("offered_stories")
    return [i for i in ids if isinstance(i, int)] if isinstance(ids, list) else []


def _story_number(value: Any) -> int | None:
    """research.json's story_id as a story number: an integer, or one written as a string
    of digits. JSON true is an int to Python and would name story 1."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().isascii() and value.strip().isdigit():
        return int(value.strip())
    return None


def read_research(workspace: Path) -> dict[str, Any]:
    try:
        data = json.loads((workspace / P.RESEARCH_FILE).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def write(ctx: Context, piece: S.Piece, note: str = "", *, resumed_reason: str = "") -> Outcome:
    # The editor's note is kept with the piece, as a revision's is, so a write that is
    # interrupted is resumed with the editor's words.
    S.update_piece(
        ctx.conn,
        piece.id,
        stage=S.STAGE_WRITING,
        error="",
        request="",
        request_note="",
        meta={"write_note": note.strip()} if note.strip() else None,
    )
    # The brief from the piece as stored now: what research found, and the writing stage
    # (so no story shortlist is fetched for a prompt that does not use one).
    piece = S.get_piece(ctx.conn, piece.id) or piece
    brief = ctx.brief_for(piece)
    text = P.write_prompt(brief, note)
    if resumed_reason:
        text = P.resume_prompt("write", resumed_reason, text)
    result = _run_stage(ctx, piece, "write", text, first=False)
    if not result.ok:
        return _fail(ctx, piece, "write", result.detail, interrupted=_stopped_early(result))
    return polish(ctx, piece)


def revise(ctx: Context, piece: S.Piece, note: str) -> Outcome:
    # A queue draft that was approved, rejected or posted since the editor asked cannot be
    # replaced: say so now rather than after an hour of revising.
    blocked = ctx.revisable(piece) if ctx.revisable else ""
    if blocked:
        S.update_piece(ctx.conn, piece.id, error=blocked, request="", request_note="")
        _write_log(Path(piece.workspace), f"!!! revise not started: {blocked}")
        return Outcome(stage=piece.stage, message=f"revision not started: {blocked}")
    # The editor may have changed the draft in the queue since the session wrote it: the
    # revision starts from the editor's version, or it would put the old text back.
    edits = ctx.queue_edits(piece) if ctx.queue_edits else None
    hand: dict[str, Any] = {}
    if edits:
        hand = _take_hand_edits(ctx, piece, edits)
    # The note is kept with the piece so a revision that is interrupted can be resumed with
    # the editor's words, not just re-checked.
    S.update_piece(
        ctx.conn,
        piece.id,
        stage=S.STAGE_REVISING,
        error="",
        request="",
        request_note="",
        meta={"revise_note": note},
    )
    text = P.revise_prompt(
        note,
        edited_posts=hand.get("files") or [],
        dropped_cards=[_card_line(c) for c in (edits.dropped if edits else [])],
    )
    result = _run_stage(ctx, piece, "revise", text, first=False)
    if not result.ok:
        return _fail(ctx, piece, "revise", result.detail, interrupted=_stopped_early(result))
    return polish(ctx, piece)


def _take_hand_edits(ctx: Context, piece: S.Piece, edits: ingest.HandEdits) -> dict[str, Any]:
    """Put the editor's queue changes into the session's files, once: a resumed revision
    finds them recorded (`hand_edit` in meta) and leaves the session's work since alone,
    unless the editor changed the draft again meanwhile. Returns that record."""
    if not ingest.unsynced(piece, edits):
        return dict(piece.meta["hand_edit"])
    files = write_back(Path(piece.workspace), edits)
    mark = {**edits.key(), "files": files}
    S.update_piece(ctx.conn, piece.id, meta={"hand_edit": mark})
    _write_log(
        Path(piece.workspace),
        "the editor's changes in the queue were written into the piece: "
        + (f"text in {', '.join(files)}" if edits.posts is not None else "text unchanged")
        + (f"; {len(edits.dropped)} card(s) dropped" if edits.dropped else ""),
    )
    return mark


def _card_line(card: dict[str, Any]) -> str:
    alt = " ".join(str(card.get("alt") or "").split())
    return str(card.get("file") or "?") + (f' ("{alt[:160]}")' if alt else "")


def write_back(workspace: Path, edits: ingest.HandEdits) -> list[str]:
    """Write the editor's queue changes into the piece's files before a revision: the text
    into the post files piece.json lists (a new file for a post the editor added, the list
    cut for one removed) and each dropped card out of piece.json's card list (its files stay
    on disk, so the session can bring it back if the editor's note asks). Returns the post
    files that hold the editor's text ([] when the text was not changed)."""
    path = workspace / P.PIECE_FILE
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        data = None
    if not isinstance(data, dict):
        data = None  # unreadable: the posts are still written, and the prompt names them
    files: list[str] = []
    if edits.posts is not None:
        listed = data.get("posts") if data is not None else None
        names = [str(n) for n in listed] if isinstance(listed, list) else []
        for i, text in enumerate(edits.posts):
            rel = names[i] if i < len(names) and qa.inside(workspace, names[i]) else ""
            if not rel:
                k = i + 1
                while f"{P.POSTS_DIR}/{k:02d}.txt" in names + files:
                    k += 1
                rel = f"{P.POSTS_DIR}/{k:02d}.txt"
            target = qa.inside(workspace, rel)
            assert target is not None  # a listed name inside the folder, or one made here
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text.rstrip() + "\n", encoding="utf-8")
            files.append(rel)
        if data is not None:
            data["posts"] = files
    if data is not None and edits.dropped and isinstance(data.get("cards"), list):
        gone = {qa.inside(workspace, str(c.get("file") or "")) for c in edits.dropped}
        gone.discard(None)
        data["cards"] = [
            c
            for c in data["cards"]
            if not (isinstance(c, dict) and qa.inside(workspace, str(c.get("file") or "")) in gone)
        ]
    if data is not None:
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        tmp.replace(path)
    return files


def polish(ctx: Context, piece: S.Piece) -> Outcome:
    """Check, hand problems back, repeat; then put the piece in the queue. One review
    round always runs so the session looks at its rendered cards at least once."""
    workspace = Path(piece.workspace)
    max_rounds = max(1, int(ctx.cfg.get("max_polish_rounds") or 1))
    S.update_piece(ctx.conn, piece.id, stage=S.STAGE_POLISHING)
    report = qa.check_piece(
        workspace,
        ctx.cfg["x"],
        ctx.known_handles,
        ctx.renderer,
        requested_angle=piece.requested_angle,
    )
    if ctx.renderer is None and report.piece is not None and report.piece.cards:
        # This machine's problem, not the piece's: a round would ask the session to fix it,
        # and the one fix in its reach is to drop its cards. Stop; Resume once a browser is in.
        S.update_piece(
            ctx.conn, piece.id, meta={"problems": report.problems, "warnings": report.warnings}
        )
        return _fail(ctx, piece, "polish", f"{qa.NO_BROWSER}; install one, then Resume")
    reviewed = not report.pictures  # nothing to look at: no review round needed
    round_no = 0
    while round_no < max_rounds and (not report.clean or not reviewed):
        round_no += 1
        review_only = report.clean
        _write_log(
            workspace,
            f"check: {len(report.blocking)} blocking, {len(report.fixable)} fixable"
            + (" (card review)" if review_only else ""),
        )
        text = P.polish_prompt(
            round_no=round_no,
            max_rounds=max_rounds,
            problems=report.problems,
            pictures=report.pictures,
            review_only=review_only,
        )
        result = _run_stage(ctx, piece, "polish", text, first=False)
        if not result.ok:
            return _fail(ctx, piece, "polish", result.detail, interrupted=_stopped_early(result))
        reviewed = True
        report = qa.check_piece(
            workspace,
            ctx.cfg["x"],
            ctx.known_handles,
            ctx.renderer,
            requested_angle=piece.requested_angle,
        )
    if report.blocking:
        message = "still blocked after polishing: " + "; ".join(report.blocking[:5])
        S.update_piece(
            ctx.conn, piece.id, meta={"problems": report.problems, "warnings": report.warnings}
        )
        return _fail(ctx, piece, "polish", message)
    pf = report.piece
    report.warnings += [f"unresolved: {p}" for p in report.fixable]
    try:
        draft_id = ctx.ingest(S.get_piece(ctx.conn, piece.id) or piece, report)
    except Exception as exc:  # the queue refused it (a revised draft no longer pending)
        log.warning("piece %s could not go into the queue: %s", piece.id, exc)
        return _fail(ctx, piece, "polish", f"could not put the piece in the queue: {exc}")
    S.update_piece(
        ctx.conn,
        piece.id,
        stage=S.STAGE_READY,
        draft_id=draft_id,
        error="",
        angle=pf.angle if pf else "",
        shape=pf.shape if pf else "",
        hook_style=pf.hook_style if pf else "",
        title=(pf.title if pf and pf.title else piece.title),
        meta={"warnings": report.warnings, "problems": []},
    )
    _write_log(workspace, f"=== ready: draft {draft_id} ===")
    return Outcome(S.STAGE_READY, f"in the queue as draft {draft_id}", report=report)


def resume_interrupted(ctx: Context, piece: S.Piece, note: str = "") -> Outcome:
    """Pick a failed or interrupted piece up where it stopped."""
    stage = str(piece.meta.get("failed_stage") or "")
    reason = piece.error or "the run stopped"
    if stage == "research":
        started = claude_cli.session_started(Path(piece.workspace) / TRANSCRIPT, piece.session_id)
        if not started:
            # Killed before the CLI recorded the session: nothing to resume, so start a new
            # session (a fresh id, in case the CLI kept a half-made one under the old id).
            S.update_piece(ctx.conn, piece.id, session_id=str(uuid.uuid4()))
            piece = S.get_piece(ctx.conn, piece.id) or piece
            return research(ctx, piece, first=True, note=note)
        return research(ctx, piece, first=False, resumed_reason=reason, note=note)
    if stage in ("write", ""):
        note = note or str(piece.meta.get("write_note") or "")
        return write(ctx, piece, note, resumed_reason=reason)
    if stage == "revise":
        note = note or str(piece.meta.get("revise_note") or "")
    if note:
        # Words typed with Resume on a piece stopped while polishing are changes asked for:
        # a revision carries them to the session, then polishes as before.
        return revise(ctx, piece, note)
    if ctx.queue_edits and ingest.unsynced(piece, ctx.queue_edits(piece)):
        # The editor changed the queue draft while the piece was stopped: polishing alone
        # would put the session's text back over it, so the session gets the changes first.
        return revise(ctx, piece, "")
    return polish(ctx, piece)
