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
import shutil
import sqlite3
import uuid
from collections.abc import Callable
from contextlib import AbstractContextManager, nullcontext
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import claude_cli
from approval_queue import store as queue_store
from studio import ingest, qa
from studio import prompt as P
from studio import store as S
from studio import topics as T
from studio.settings import stage_max_turns, stage_timeout

log = logging.getLogger(__name__)

TRANSCRIPT = "session.ndjson"
LOG_FILE = "session.log"
# After folder_missing's words, for a piece that stopped (a finished one is revised instead).
PUT_BACK = "; put it back, then Resume, or discard the piece"

Launcher = Callable[..., claude_cli.SessionResult]


@dataclass
class Context:
    """What a stage run needs besides the piece: settings, the system prompt, the brief
    builder and the plumbing."""

    conn: sqlite3.Connection
    cfg: dict[str, Any]
    root_cfg: dict[str, Any] | None
    system: str
    # The shipped reference pieces (studio/exemplars), which every stage copies into the
    # piece's folder (copy_reference) rather than opening to the session.
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
    # The studio's claim lock (runner.claiming), held while a piece's shortlist is chosen
    # and its offers recorded, since several pieces may be researched at once; a no-op here.
    claiming: Callable[[], AbstractContextManager[Any]] = nullcontext


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


def _echo(ctx: Context, line: str) -> None:
    """Show a line on the run's output, never at the cost of the lines after it. A console
    that cannot show a character raises from print (a Windows pipe is cp1252 and strict,
    and a session's narration is full of arrows, >= signs and Greek letters), and one
    event's lines are said in a loop: a raise would keep the rest of them out of
    session.log too. run_studio.py makes its own print safe; this guards any echo."""
    try:
        ctx.echo(line)
    except Exception:  # a display hook must never cost the log a line
        log.debug("could not echo %r", line, exc_info=True)


def _run_stage(
    ctx: Context, piece: S.Piece, stage: str, prompt_text: str, *, first: bool
) -> claude_cli.SessionResult:
    """One CLI invocation. Records a studio_runs row around it. A CLI that cannot run at
    all leaves the piece interrupted (so it can be resumed at once) and the error goes up,
    so the step shows as failed. A piece folder that is gone or cannot be written comes
    back as a failed result instead: that is this piece's problem, and the run goes on to
    the editor's other requests. A session the CLI no longer has is taken over by a fresh
    one that reads the piece's files (`fresh_session`)."""
    # The session as stored now: an earlier run in this call may have moved the piece to a
    # fresh one, and the caller's copy of the piece predates that.
    piece = S.get_piece(ctx.conn, piece.id) or piece
    workspace = Path(piece.workspace)
    run_id = S.start_run(ctx.conn, piece.id, stage)
    _write_log(workspace, f"=== {stage} ===")
    started = False

    def say(line: str) -> None:
        _write_log(workspace, line)
        _echo(ctx, f"[piece {piece.id} {stage}] {line}")

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
        copy_reference(workspace, ctx.reference_dir)
        result = launch(
            prompt_text,
            model=piece.model,
            cwd=workspace,
            session_id=piece.session_id,
            resume=not first,
            # Every launch, resumes included: the CLI's record of the first launch's system
            # prompt lasts only until the conversation is compacted (claude_cli.run_session).
            system=ctx.system,
            effort=piece.effort or None,
            tools=ctx.cfg["tools"],
            allowed=ctx.cfg["tools"],
            # No --add-dir: under --restricted a folder added is one the file tools can
            # write, and the reference pieces are read from the piece's own copy.
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
    except OSError as exc:
        # The reference pieces could not be copied or the transcript could not be opened or
        # written: the folder was deleted, the data folder moved, the disk is full. Raised,
        # it would leave the piece mid-stage and end the run before the editor's other
        # requests and the new piece.
        message = (
            folder_missing(piece) + PUT_BACK
            if not workspace.is_dir()
            else f"could not write the session's files: {exc}"
        )
        S.finish_run(ctx.conn, run_id, outcome="error", detail=message)
        return claude_cli.SessionResult(
            session_id=piece.session_id, ok=False, subtype="error", terminal_reason=message
        )
    if not first and claude_cli.session_lost(result):
        S.finish_run(
            ctx.conn,
            run_id,
            outcome="session_lost",
            detail=f"the CLI no longer has session {piece.session_id}",
        )
        return fresh_session(ctx, piece, stage, prompt_text)
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
        cost_usd=_run_cost(ctx, piece, result),
        duration_ms=result.duration_ms,
    )
    return result


def copy_reference(workspace: Path, source: Path) -> None:
    """Give the piece its own copy of the reference pieces (P.REFERENCE_DIR in its folder)
    when it has none, before a stage runs. The session reads them there instead of in the
    shipped folder: a folder it can read under --restricted is one its Write and Edit can
    change, and a web page that talked it into editing a handoff doc would steer every later
    piece (and change files git tracks). Whatever it does to this copy stays in this piece.
    The copy is made beside the target and renamed, so one cut short is made again next
    time. A piece folder that is gone is left alone: the launch says so (folder_missing)."""
    target = workspace / P.REFERENCE_DIR
    if target.exists() or not workspace.is_dir() or not source.is_dir():
        return
    part = workspace / f"{P.REFERENCE_DIR}.part"
    shutil.rmtree(part, ignore_errors=True)
    shutil.copytree(source, part)
    part.replace(target)


def _run_cost(ctx: Context, piece: S.Piece, result: claude_cli.SessionResult) -> float:
    """What this one CLI run cost. The CLI reports the session's running total (a resumed
    session carries on from its last one), so the run's own cost is what the total grew by
    since the piece's session last reported one (`session_cost` in its meta). A new session
    starts from nothing, and a total lower than the last (a CLI that counts each launch on
    its own) is the run's own. A run that reported nothing cost nothing it can say; the next
    run's total takes it in."""
    total = float(result.cost_usd or 0.0)
    if total <= 0:
        return 0.0
    seen = piece.meta.get("session_cost")
    before = 0.0
    if isinstance(seen, dict) and seen.get("session") == piece.session_id:
        try:
            before = float(seen.get("usd") or 0.0)
        except (TypeError, ValueError):
            before = 0.0
    S.update_piece(
        ctx.conn, piece.id, meta={"session_cost": {"session": piece.session_id, "usd": total}}
    )
    return total - before if total >= before else total


def fresh_session(
    ctx: Context, piece: S.Piece, stage: str, prompt_text: str
) -> claude_cli.SessionResult:
    """Run a stage in a new session because the CLI no longer has the piece's own (its
    stored conversation was cleaned up, `claude_cli.session_lost`). Every later --resume
    would fail the same way, so the piece takes a new session id for good; the new session
    gets the standing instructions again and reads the piece's files before the stage.
    The piece's log and its runs say so."""
    old, new = piece.session_id, str(uuid.uuid4())
    S.update_piece(
        ctx.conn,
        piece.id,
        session_id=new,
        meta={"lost_sessions": [*_lost_sessions(piece), old]},
    )
    note = (
        f"the CLI no longer has session {old} (Claude Code clears old sessions); "
        f"a fresh session {new} takes the piece over from its files"
    )
    _write_log(Path(piece.workspace), note)
    _echo(ctx, f"[piece {piece.id} {stage}] {note}")
    log.warning("piece %s: %s", piece.id, note)
    piece = S.get_piece(ctx.conn, piece.id) or piece
    return _run_stage(ctx, piece, stage, P.fresh_session_prompt(stage, prompt_text), first=True)


def _lost_sessions(piece: S.Piece) -> list[str]:
    ids = piece.meta.get("lost_sessions")
    return [str(i) for i in ids] if isinstance(ids, list) else []


def folder_missing(piece: S.Piece) -> str:
    """What the piece's page says when its folder is gone ("" while it is there)."""
    if Path(piece.workspace).is_dir():
        return ""
    return f"the piece's folder {piece.workspace} is missing"


def _fail(
    ctx: Context, piece: S.Piece, stage: str, message: str, *, interrupted: bool = False
) -> Outcome:
    new_stage = S.STAGE_INTERRUPTED if interrupted else S.STAGE_FAILED
    S.update_piece(ctx.conn, piece.id, stage=new_stage, error=message, meta={"failed_stage": stage})
    _write_log(Path(piece.workspace), f"!!! {stage}: {message}")
    return Outcome(stage=new_stage, message=message)


def _stopped_early(result: claude_cli.SessionResult) -> bool:
    """A stage that ran out of turns or time, or whose CLI stopped a sub-agent before it
    reported (the cold fact-check), can be resumed rather than failed."""
    return result.subtype in ("error_max_turns", "killed", "no_result", claude_cli.SUBAGENT_KILLED)


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
    # Under the studio's claim lock: a piece researching beside this one must see these
    # offers before it builds its own shortlist, or both would be offered the same stories.
    with ctx.claiming():
        brief = ctx.brief_for(piece)
        # The stories the session may name in research.json: every one offered so far (a
        # resumed research run may be offered a newer shortlist than the first).
        offered = sorted({*_offered(piece), *(s.cluster_id for s in brief.shortlist)})
        S.update_piece(ctx.conn, piece.id, meta={"offered_stories": offered})
    write_earlier(Path(piece.workspace), brief)
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
        if story_id not in offered:
            log.warning(
                "piece %s: research.json names story %s, which was not offered", piece.id, story_id
            )
            _write_log(workspace, f"research.json names story {story_id}, not one offered; ignored")
        elif story_id in queue_store.drafted_cluster_ids(ctx.conn):
            # The story got a draft while research ran. One story, one piece of writing,
            # so the piece stops here rather than write the story a second time.
            S.update_piece(ctx.conn, piece.id, **updates)
            return _fail(
                ctx,
                piece,
                "research",
                f"story {story_id} got a draft from the drafter while this research ran, "
                "and a story gets one piece of writing; Resume to research another story "
                "from the shortlist, or discard the piece",
            )
        else:
            updates["cluster_id"] = story_id
            # so the story is still found once linking merges it into another cluster
            updates["story_item"] = T.story_item(story_id)
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


def write_earlier(workspace: Path, brief: P.Brief) -> None:
    """Put the account's recent pieces (P.earlier_pieces) in the working folder, where the
    stage prompt says they are: --restricted keeps the session out of other pieces' folders.
    Removed when there are none. A folder that cannot be written is left to the stage's
    launch, which says why."""
    path = workspace / P.EARLIER_FILE
    text = P.earlier_pieces(brief)
    try:
        if text:
            path.write_text(text, encoding="utf-8")
        else:
            path.unlink(missing_ok=True)
    except OSError as exc:
        log.warning("could not write %s: %s", path, exc)


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
    write_earlier(Path(piece.workspace), brief)
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
    blocked = (ctx.revisable(piece) if ctx.revisable else "") or folder_missing(piece)
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
    missing = folder_missing(piece)
    if missing:  # nothing to resume from, and no session can be recorded there
        return _fail(ctx, piece, stage or "write", missing + PUT_BACK)
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
    # Stopped while polishing a revision (or between the queue and `ready`) and the draft
    # it would replace was approved or posted meanwhile: the queue would refuse the result,
    # so no polish round is spent on it. The piece stays where it was and says why.
    blocked = ctx.revisable(piece) if ctx.revisable else ""
    if blocked:
        S.update_piece(ctx.conn, piece.id, error=blocked)
        _write_log(Path(piece.workspace), f"!!! polish not resumed: {blocked}")
        return Outcome(stage=piece.stage, message=f"not resumed: {blocked}")
    return polish(ctx, piece)
