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
from studio import prompt as P
from studio import qa
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
    """One CLI invocation. Records a studio_runs row around it."""
    workspace = Path(piece.workspace)
    run_id = S.start_run(ctx.conn, piece.id, stage)
    _write_log(workspace, f"=== {stage} ===")

    def on_event(event: dict[str, Any]) -> None:
        for line in claude_cli.describe_event(event):
            _write_log(workspace, line)
            ctx.echo(f"[piece {piece.id} {stage}] {line}")

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
        raise
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
    ctx: Context, piece: S.Piece, *, first: bool = True, resumed_reason: str = ""
) -> Outcome:
    S.update_piece(ctx.conn, piece.id, stage=S.STAGE_RESEARCHING, error="")
    piece = S.get_piece(ctx.conn, piece.id) or piece
    brief = ctx.brief_for(piece)
    text = P.research_prompt(brief)
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
    story_id = info.get("story_id")
    if isinstance(story_id, int) and piece.cluster_id is None:
        updates["cluster_id"] = story_id
    S.update_piece(ctx.conn, piece.id, **updates)
    piece = S.get_piece(ctx.conn, piece.id) or piece
    if piece.checkpoint or ctx.stop_after_research:
        S.update_piece(ctx.conn, piece.id, stage=S.STAGE_RESEARCH_READY)
        return Outcome(S.STAGE_RESEARCH_READY, "research done; waiting for the editor")
    return write(ctx, piece)


def read_research(workspace: Path) -> dict[str, Any]:
    try:
        data = json.loads((workspace / P.RESEARCH_FILE).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def write(ctx: Context, piece: S.Piece, note: str = "", *, resumed_reason: str = "") -> Outcome:
    brief = ctx.brief_for(piece)
    text = P.write_prompt(brief, note)
    if resumed_reason:
        text = P.resume_prompt("write", resumed_reason, text)
    S.update_piece(ctx.conn, piece.id, stage=S.STAGE_WRITING, error="", request="", request_note="")
    result = _run_stage(ctx, piece, "write", text, first=False)
    if not result.ok:
        return _fail(ctx, piece, "write", result.detail, interrupted=_stopped_early(result))
    return polish(ctx, piece)


def revise(ctx: Context, piece: S.Piece, note: str) -> Outcome:
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
    result = _run_stage(ctx, piece, "revise", P.revise_prompt(note), first=False)
    if not result.ok:
        return _fail(ctx, piece, "revise", result.detail, interrupted=_stopped_early(result))
    return polish(ctx, piece)


def polish(ctx: Context, piece: S.Piece) -> Outcome:
    """Check, hand problems back, repeat; then put the piece in the queue. One review
    round always runs so the session looks at its rendered cards at least once."""
    workspace = Path(piece.workspace)
    max_rounds = max(1, int(ctx.cfg.get("max_polish_rounds") or 1))
    S.update_piece(ctx.conn, piece.id, stage=S.STAGE_POLISHING)
    report = qa.check_piece(workspace, ctx.cfg["x"], ctx.known_handles, ctx.renderer)
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
        report = qa.check_piece(workspace, ctx.cfg["x"], ctx.known_handles, ctx.renderer)
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
            return research(ctx, piece, first=True)
        return research(ctx, piece, first=False, resumed_reason=reason)
    if stage in ("write", ""):
        return write(ctx, piece, note, resumed_reason=reason)
    if stage == "revise":
        note = note or str(piece.meta.get("revise_note") or "")
        if note:
            return revise(ctx, piece, note)
    return polish(ctx, piece)
