"""Step 10's runner (studio/runner.py), its CLI (run_studio.py) and the feed stories it
offers a session (studio/topics.py).

What one studio run does around the sessions: mark pieces a dead run left mid-stage,
act on the editor's requests, decide whether to start a new piece and on what (an
explicit topic or story, then a queued topic, then `--now`, then the automatic limits),
and hold the studio lock while it does. The Claude Code CLI is faked (`FakeCLI` in place
of claude_cli.run_session, which tests/conftest.py otherwise refuses): it tells the stage
from the first line of its prompt and writes that stage's files in the piece's folder. No
test draws a card (the pieces here have none, and make_renderer is replaced by one that is
never asked; `rig.renderer = None` is a machine without a browser), and the DB
is a temp file (DB_PATH), so the data folder, the workspace root and the lock all live in
tmp_path.
"""

from __future__ import annotations

import json
import logging
import re
import shutil
import threading
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest

import claude_cli
import run_studio
import timeutil
from approval_queue import store as queue_store
from db import Database
from draft.schema import SHAPE_LONG, Draft
from ops import lock
from publish import store as publish_store
from studio import angles as A
from studio import prompt as P
from studio import qa, runner
from studio import render as render_mod
from studio import session as SS
from studio import store as S
from studio import topics as T
from studio.settings import EXEMPLARS_DIR, load_studio_config
from tests.conftest import ABSTRACT, URL, seed_item

T0 = datetime(2026, 10, 5, 12, 0, tzinfo=UTC)
ZONE = "America/Los_Angeles"
TOPIC = "next-gen CTLA-4"
NOTE = "Lead with the durability, drop the history."
TITLE = "A written-off bispecific is back"
POST = (
    "A second-line phase 2 readout put a response rate of 41% on the table for a "
    "bispecific most of the field had written off.\n\nNot investment advice."
)


# ---- fixtures ------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _no_dotenv(monkeypatch):
    """run_studio.main loads .env; the developer's must not reach a test."""
    monkeypatch.setattr(run_studio, "load_dotenv", lambda *a, **k: False)


@pytest.fixture(autouse=True)
def _display_zone(monkeypatch):
    """Dates a human (or the session) reads are in this zone, whatever config.yaml says."""
    monkeypatch.setattr(timeutil, "timezone_name", lambda: ZONE)


@pytest.fixture
def sconn(conn):
    """The shared pipeline DB (step 1's tables, the queue's) with the studio's tables."""
    S.ensure_tables(conn)
    return conn


@pytest.fixture
def cfg() -> dict[str, Any]:
    c = load_studio_config()
    c["auto"] = {"enabled": True, "max_new_per_day": 1, "min_hours_between": 6, "checkpoint": False}
    c["manual"] = {"checkpoint": True}
    c["variety"] = {"avoid_recent_angles": 3, "avoid_recent_hooks": 2, "recent_pieces_shown": 8}
    c["topics"] = {"lookback_hours": 48, "shortlist": 8, "min_score": 30, "avoid_days": 10}
    c["x"] = {
        "long_post_max": 25000,
        "thread_post_max": 25000,
        "short_post_max": 1000,
        "headroom": 50,
        "max_cards_total": 4,
    }
    c["workspace_dir"] = "studio_pieces"
    return c


class Clock:
    """A settable stand-in for studio/store.py's _now (aware UTC, seconds)."""

    def __init__(self) -> None:
        self.now = T0

    def __call__(self) -> str:
        return self.now.isoformat(timespec="seconds")

    def set(self, when: datetime) -> None:
        self.now = when


@pytest.fixture
def clock(monkeypatch) -> Clock:
    c = Clock()
    monkeypatch.setattr(S, "_now", c)
    return c


def make_piece(
    conn,
    *,
    stage: str = S.STAGE_RESEARCHING,
    origin: str = S.ORIGIN_MANUAL,
    topic: str = TOPIC,
    cluster_id: int | None = None,
    requested_angle: str = "",
    workspace: Path | str = "/nowhere/studio_pieces/piece",
    meta: dict[str, Any] | None = None,
    **fields: Any,
) -> S.Piece:
    """A studio_pieces row at `stage`, with any other updatable fields set."""
    pid = S.create_piece(
        conn,
        origin=origin,
        topic=topic,
        cluster_id=cluster_id,
        requested_angle=requested_angle,
        checkpoint=True,
        session_id=str(uuid.uuid4()),
        workspace=str(workspace),
        model="writer-model",
        effort="max",
    )
    if stage != S.STAGE_RESEARCHING:
        fields["stage"] = stage
    if fields or meta is not None:
        S.update_piece(conn, pid, meta=meta, **fields)
    return get(conn, pid)


def get(conn, piece_id: int) -> S.Piece:
    piece = S.get_piece(conn, piece_id)
    assert piece is not None
    return piece


def merge(keep: int, *others: int) -> None:
    """Story linking (filter/link.py) folding `others` into `keep`, through step 1's own
    Database on the test DB: the items move over and the other clusters are deleted."""
    database = Database(str(queue_store.db_path()))
    try:
        database.merge_clusters(keep, list(others))
    finally:
        database.close()


def studio_draft(conn, piece: S.Piece) -> int:
    """The piece's pending draft in the approval queue, as studio/ingest.py inserts it."""
    draft_id = queue_store.insert_draft(
        conn,
        item_id=queue_store.studio_item_id(piece.id),
        model="writer-model (studio)",
        draft=Draft(thread=[POST], suggested_visual="", why_it_matters="w"),
    )
    S.update_piece(conn, piece.id, draft_id=draft_id)
    return draft_id


# ---- what a session writes, and the fake CLI ------------------------------------------


def write_research(ws: Path) -> None:
    """The research stage's files: the fact base and research.json."""
    ws.mkdir(parents=True, exist_ok=True)
    (ws / P.FACTBASE_FILE).write_text("# Fact base\n\nAs of 2026-10-05.\n", encoding="utf-8")
    info = {
        "topic": TITLE,
        "story_id": None,
        "why_now": "The readout landed this week.",
        "companies": [{"name": "Merck", "ticker": "MRK"}],
        "candidate_angles": [{"angle": "class_deep_dive", "why": "the class is back"}],
        "summary": "A phase 2 readout reopens a class.",
    }
    (ws / P.RESEARCH_FILE).write_text(json.dumps(info), encoding="utf-8")


def write_piece(ws: Path) -> None:
    """One clean long post without cards: the checker passes it at once."""
    (ws / P.POSTS_DIR).mkdir(parents=True, exist_ok=True)
    (ws / P.POSTS_DIR / "01.txt").write_text(POST, encoding="utf-8")
    (ws / P.FACTCHECK_FILE).write_text("| post | claim | problem |\n|---|---|---|\n", "utf-8")
    piece = {
        "title": TITLE,
        "angle": "class_deep_dive",
        "angle_reason": "the readout reopens a class",
        "shape": "long_post",
        "hook_style": "hard_number",
        "posts": [f"{P.POSTS_DIR}/01.txt"],
        "cards": [],
        "companies": [{"name": "Merck", "ticker": "MRK"}],
        "handles": [],
        "recheck_before_posting": [],
        "summary": "One long post on the readout.",
    }
    (ws / P.PIECE_FILE).write_text(json.dumps(piece), encoding="utf-8")


HEADS = {
    "STAGE 1 OF 3: RESEARCH": "research",
    "STAGE 2 OF 3: WRITE": "write",
    "STAGE 3 OF 3: POLISH": "polish",
    "REVISION": "revise",
}


def stage_of(prompt: str) -> str:
    body = prompt.split("The stage's instructions, again:", 1)[-1].lstrip()
    for head, stage in HEADS.items():
        if body.startswith(head):
            return stage
    raise AssertionError(f"not a stage prompt: {prompt[:80]!r}")


class FakeCLI:
    """claude_cli.run_session's stand-in: writes each stage's files, returns a result."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict[str, Any]]] = []
        self.results: dict[str, claude_cli.SessionResult] = {}

    def stages(self) -> list[str]:
        return [stage for stage, _, _ in self.calls]

    def prompt(self, stage: str) -> str:
        return next(p for s, p, _ in self.calls if s == stage)

    def kw(self, stage: str) -> dict[str, Any]:
        return next(k for s, _, k in self.calls if s == stage)

    def __call__(self, prompt: str, **kw: Any) -> claude_cli.SessionResult:
        stage = stage_of(prompt)
        self.calls.append((stage, prompt, kw))
        ws = Path(kw["cwd"])
        if not ws.is_dir():  # the real launcher opens the transcript there before it starts
            raise FileNotFoundError(2, "No such file or directory", str(kw["transcript"]))
        if stage in self.results:
            return self.results[stage]
        if stage == "research":
            write_research(ws)
        elif stage in ("write", "revise"):
            write_piece(ws)
        return claude_cli.SessionResult(
            session_id=kw["session_id"],
            ok=True,
            subtype="success",
            text=f"{stage} done",
            num_turns=3,
            cost_usd=1.5,
            duration_ms=60_000,
            returncode=0,
        )


def never_draws(html: Path, png: Path) -> render_mod.RenderResult:
    """The rig machine's card browser: found, and never asked (no rig piece has cards)."""
    raise AssertionError(f"a card was drawn: {html}")


class Rig:
    def __init__(self, conn, cli: FakeCLI, root: Path) -> None:
        self.conn = conn
        self.cli = cli
        self.root = root  # the workspace root, where the studio lock lives too
        # What make_renderer finds on this machine; None: no browser to draw cards with.
        self.renderer: qa.Renderer | None = never_draws

    def pieces(self) -> list[S.Piece]:
        return S.list_pieces(self.conn)

    def only_piece(self) -> S.Piece:
        pieces = self.pieces()
        assert len(pieces) == 1, [p.id for p in pieces]
        return pieces[0]


@pytest.fixture
def rig(sconn, cfg, monkeypatch, tmp_path) -> Rig:
    """runner.run() over the temp DB with the fake CLI and the test's own settings."""
    cli = FakeCLI()
    rig = Rig(sconn, cli, tmp_path / "studio_pieces")
    monkeypatch.setattr(runner, "load_studio_config", lambda *a, **k: cfg)
    monkeypatch.setattr(claude_cli, "run_session", cli)
    monkeypatch.setattr(runner, "make_renderer", lambda c: rig.renderer)
    # a run that finds the studio lock taken waits this long for it (10 s shipped)
    monkeypatch.setattr(runner, "LOCK_WAIT_SECONDS", 0.3)
    return rig


# ---- allowed_to_start: the automatic run's limits --------------------------------------


def test_automatic_pieces_can_be_switched_off(sconn, cfg):
    cfg["auto"]["enabled"] = False
    ok, why = runner.allowed_to_start(sconn, cfg, T0)
    assert ok is False
    assert why == "automatic pieces are off (studio/config.yaml auto.enabled)"


def test_an_empty_studio_may_start_a_piece(sconn, cfg):
    assert runner.allowed_to_start(sconn, cfg, T0) == (True, "")


def test_the_daily_cap_counts_only_automatic_pieces_of_the_local_calendar_day(sconn, cfg, clock):
    """`max_new_per_day` counts the automatic pieces started on today's date in the display
    zone (Los Angeles here), the zone the automatic run times are typed in."""
    cfg["auto"].update(max_new_per_day=2, min_hours_between=0)
    clock.set(datetime(2026, 10, 5, 6, 59, tzinfo=UTC))  # 23:59 on 10-04 in Los Angeles
    make_piece(sconn, origin=S.ORIGIN_AUTO, stage=S.STAGE_READY)
    clock.set(datetime(2026, 10, 5, 7, 0, tzinfo=UTC))  # midnight: 10-05 there
    make_piece(sconn, origin=S.ORIGIN_AUTO, stage=S.STAGE_READY)
    for _ in range(3):  # the editor's pieces never use up the automatic allowance
        make_piece(sconn, origin=S.ORIGIN_MANUAL, stage=S.STAGE_READY)
    morning = datetime(2026, 10, 5, 13, 5, tzinfo=UTC)  # 06:05 on 10-05
    assert runner.allowed_to_start(sconn, cfg, morning) == (True, "")

    clock.set(morning)
    make_piece(sconn, origin=S.ORIGIN_AUTO, stage=S.STAGE_READY)
    blocked = (False, "2 automatic piece(s) today (limit 2)")
    assert runner.allowed_to_start(sconn, cfg, morning) == blocked
    # the rest of that day stays blocked; the next day starts with a fresh allowance
    late = datetime(2026, 10, 6, 6, 59, 59, tzinfo=UTC)  # 23:59:59 on 10-05
    assert runner.allowed_to_start(sconn, cfg, late) == blocked
    next_day = datetime(2026, 10, 6, 7, 0, tzinfo=UTC)
    assert runner.allowed_to_start(sconn, cfg, next_day) == (True, "")


def test_one_automatic_piece_a_day_whatever_minute_the_run_reaches_the_studio(sconn, cfg, clock):
    """The shipped pace at the shipped run times (06:00, 12:00 and 18:00 local, the studio
    step a few minutes after each, sooner some days than others): a piece every day at the
    day's first run time. Counted over a rolling 24 hours, 06:03 was refused because
    yesterday's piece started at 06:06, so the piece slid through the day, and each slide
    off the evening run lost a whole day."""
    cfg["auto"].update(max_new_per_day=1, min_hours_between=6)
    zone = ZoneInfo(ZONE)
    started = []
    for day in range(10):
        delay = 6 if day % 2 == 0 else 3  # minutes after the run time
        for hour in (6, 12, 18):
            when = datetime(2026, 10, 5 + day, hour, delay, tzinfo=zone).astimezone(UTC)
            if runner.allowed_to_start(sconn, cfg, when)[0]:
                clock.set(when)
                make_piece(sconn, origin=S.ORIGIN_AUTO, stage=S.STAGE_READY)
                started.append(when.astimezone(zone).strftime("%m-%d %H:%M"))
    assert started == [f"10-{5 + d:02d} 06:0{6 if d % 2 == 0 else 3}" for d in range(10)]


def test_the_daily_cap_follows_the_display_zone(sconn, cfg, clock):
    """The same instant is another day in another zone: 01:00 UTC on 10-06 is still 10-05
    in Los Angeles, where a piece started at 08:00 UTC on 10-05 used up the day."""
    clock.set(datetime(2026, 10, 5, 8, 0, tzinfo=UTC))
    make_piece(sconn, origin=S.ORIGIN_AUTO, stage=S.STAGE_READY)
    cfg["auto"].update(max_new_per_day=1, min_hours_between=0)
    when = datetime(2026, 10, 6, 1, 0, tzinfo=UTC)
    assert runner.allowed_to_start(sconn, cfg, when)[0] is False
    assert runner.allowed_to_start(sconn, cfg, when, ZoneInfo("UTC")) == (True, "")


def test_a_cap_of_zero_starts_nothing_automatically(sconn, cfg):
    cfg["auto"]["max_new_per_day"] = 0
    assert runner.allowed_to_start(sconn, cfg, T0) == (
        False,
        "0 automatic piece(s) today (limit 0)",
    )


def test_the_gap_counts_from_the_last_piece_of_any_origin(sconn, cfg, clock):
    cfg["auto"].update(max_new_per_day=5, min_hours_between=6)
    clock.set(T0 - timedelta(hours=5))
    make_piece(sconn, origin=S.ORIGIN_MANUAL, stage=S.STAGE_DISCARDED)
    assert runner.allowed_to_start(sconn, cfg, T0) == (
        False,
        "the last piece started under 6 hours ago",
    )
    # exactly six hours on is no longer "under six hours"
    assert runner.allowed_to_start(sconn, cfg, T0 + timedelta(hours=1)) == (True, "")


def test_a_gap_of_zero_lets_pieces_start_back_to_back(sconn, cfg, clock):
    cfg["auto"].update(max_new_per_day=5, min_hours_between=0)
    make_piece(sconn, origin=S.ORIGIN_AUTO, stage=S.STAGE_READY)
    assert runner.allowed_to_start(sconn, cfg, T0) == (True, "")


def test_a_piece_waiting_at_the_research_checkpoint_blocks_a_new_one(sconn, cfg, clock):
    cfg["auto"].update(max_new_per_day=5, min_hours_between=0)
    clock.set(T0 - timedelta(days=3))
    waiting = make_piece(sconn, stage=S.STAGE_RESEARCH_READY)
    make_piece(sconn, stage=S.STAGE_READY)
    assert runner.allowed_to_start(sconn, cfg, T0) == (
        False,
        f"piece {waiting.id} is waiting at the research checkpoint",
    )
    S.update_piece(sconn, waiting.id, stage=S.STAGE_DISCARDED)  # the editor gave up on it
    assert runner.allowed_to_start(sconn, cfg, T0) == (True, "")


def test_a_piece_waiting_at_the_checkpoint_blocks_however_many_came_after_it(sconn, cfg, clock):
    cfg["auto"].update(max_new_per_day=5, min_hours_between=0)
    clock.set(T0 - timedelta(days=30))
    waiting = make_piece(sconn, stage=S.STAGE_RESEARCH_READY)
    for _ in range(30):
        make_piece(sconn, stage=S.STAGE_READY)
    assert runner.allowed_to_start(sconn, cfg, T0) == (
        False,
        f"piece {waiting.id} is waiting at the research checkpoint",
    )


# ---- mark_stale: what a dead run left behind -----------------------------------------


RUNNING = {
    S.STAGE_RESEARCHING: "research",
    S.STAGE_WRITING: "write",
    S.STAGE_POLISHING: "polish",
    S.STAGE_REVISING: "revise",
}
SETTLED = (
    S.STAGE_RESEARCH_READY,
    S.STAGE_READY,
    S.STAGE_FAILED,
    S.STAGE_INTERRUPTED,
    S.STAGE_DISCARDED,
)


def test_pieces_left_mid_stage_are_interrupted_with_their_open_runs_closed(sconn):
    running = {
        stage: make_piece(sconn, stage=stage, meta={"research": {"topic": "kept"}})
        for stage in RUNNING
    }
    settled = {stage: make_piece(sconn, stage=stage) for stage in SETTLED}
    for piece in [*running.values(), *settled.values()]:
        done = S.start_run(sconn, piece.id, "research")
        S.finish_run(sconn, done, outcome="ok", detail="finished")
    for stage, piece in running.items():
        S.start_run(sconn, piece.id, RUNNING[stage])  # the run that died

    stale = runner.mark_stale(sconn)

    assert sorted(p.id for p in stale) == sorted(p.id for p in running.values())
    for stage, name in RUNNING.items():
        piece = get(sconn, running[stage].id)
        assert piece.stage == S.STAGE_INTERRUPTED
        assert piece.error == (
            f"the {name} stage stopped before it finished (stopped, timed out or crashed)"
        )
        assert piece.meta == {"research": {"topic": "kept"}, "failed_stage": name}
        first, last = S.list_runs(sconn, piece.id)
        assert (first.outcome, first.detail) == ("ok", "finished")
        assert last.outcome == "interrupted" and last.finished_at is not None
        assert last.detail == "the run stopped before the stage finished"
    for stage, piece in settled.items():
        after = get(sconn, piece.id)
        assert (after.stage, after.error, after.meta) == (stage, "", {})
        assert [r.outcome for r in S.list_runs(sconn, piece.id)] == ["ok"]
    assert runner.mark_stale(sconn) == []


# ---- act_on_requests: the editor's Continue, Revise and Resume ------------------------


def bare_context(conn, cfg) -> SS.Context:
    """A Context whose plumbing must not be reached: the stages are replaced."""

    def unreachable(*a: Any, **k: Any) -> Any:
        raise AssertionError("a stage ran for real")

    return SS.Context(
        conn=conn,
        cfg=cfg,
        root_cfg=None,
        system="",
        reference_dir=EXEMPLARS_DIR,
        known_handles=set(),
        brief_for=unreachable,
        renderer=None,
        ingest=unreachable,
        launch=unreachable,
        echo=lambda line: None,
    )


@pytest.fixture
def stages(monkeypatch) -> list[tuple[str, int, str, str]]:
    """session.write / resume_interrupted / revise replaced by recorders. Each call notes
    the stage, the piece, the note and the request as stored while the stage runs."""
    calls: list[tuple[str, int, str, str]] = []

    def recorder(name: str):
        def stage(ctx: SS.Context, piece: S.Piece, note: str = "", **kw: Any) -> SS.Outcome:
            stored = get(ctx.conn, piece.id)
            calls.append((name, piece.id, note, stored.request + stored.request_note))
            return SS.Outcome(S.STAGE_READY, f"{name} done")

        return stage

    monkeypatch.setattr(SS, "write", recorder("write"))
    monkeypatch.setattr(SS, "resume_interrupted", recorder("resume"))
    monkeypatch.setattr(SS, "revise", recorder("revise"))
    return calls


@pytest.mark.parametrize(
    "stage, what, called",
    [
        (S.STAGE_RESEARCH_READY, S.REQUEST_CONTINUE, "write"),
        (S.STAGE_FAILED, S.REQUEST_CONTINUE, "resume"),
        (S.STAGE_INTERRUPTED, S.REQUEST_CONTINUE, "resume"),
        (S.STAGE_READY, S.REQUEST_REVISE, "revise"),
    ],
)
def test_each_request_goes_to_its_stage_with_the_editors_note(
    sconn, cfg, stages, stage, what, called
):
    piece = make_piece(sconn, stage=stage)
    if stage == S.STAGE_READY:
        studio_draft(sconn, piece)
    S.request(sconn, piece.id, what, f"  {NOTE}  ")
    assert runner.act_on_requests(bare_context(sconn, cfg)) == 1
    # the request is cleared before the stage runs, so a stage that dies is not re-run by
    # every later studio run; the stage keeps the note itself
    assert stages == [(called, piece.id, NOTE, "")]


def test_a_finished_piece_without_a_queue_draft_is_still_revised(sconn, cfg, stages):
    """studio/ingest.py inserts a fresh draft when the piece has none, so it can land."""
    piece = make_piece(sconn, stage=S.STAGE_READY)
    S.request(sconn, piece.id, S.REQUEST_REVISE, NOTE)
    assert runner.act_on_requests(bare_context(sconn, cfg)) == 1
    assert stages == [("revise", piece.id, NOTE, "")]


def test_a_revision_without_words_asks_for_an_improvement(sconn, cfg, stages):
    piece = make_piece(sconn, stage=S.STAGE_READY)
    studio_draft(sconn, piece)
    S.request(sconn, piece.id, S.REQUEST_REVISE, "   ")
    runner.act_on_requests(bare_context(sconn, cfg))
    assert stages == [("revise", piece.id, "Improve the piece.", "")]


@pytest.mark.parametrize(
    "stage, what",
    [
        (S.STAGE_READY, S.REQUEST_CONTINUE),
        (S.STAGE_RESEARCH_READY, S.REQUEST_REVISE),
        (S.STAGE_FAILED, S.REQUEST_REVISE),
        (S.STAGE_INTERRUPTED, S.REQUEST_REVISE),
        (S.STAGE_DISCARDED, S.REQUEST_CONTINUE),
        (S.STAGE_DISCARDED, S.REQUEST_REVISE),
        (S.STAGE_RESEARCHING, S.REQUEST_CONTINUE),
        (S.STAGE_WRITING, S.REQUEST_REVISE),
    ],
)
def test_a_request_that_does_not_fit_the_stage_is_cleared_and_ignored(
    sconn, cfg, stages, stage, what
):
    piece = make_piece(sconn, stage=stage)
    S.request(sconn, piece.id, what, NOTE)
    assert runner.act_on_requests(bare_context(sconn, cfg)) == 0
    assert stages == []
    after = get(sconn, piece.id)
    assert (after.stage, after.request, after.request_note, after.error) == (stage, "", "", "")
    assert S.pending_requests(sconn) == []


def test_a_revision_is_not_run_once_its_draft_is_approved(sconn, cfg, stages):
    """The editor asked for a revision, then approved the draft before a studio run picked
    the request up: the queue would refuse the revised text, so no session is spent and the
    finished piece stays ready, saying why."""
    piece = make_piece(sconn, stage=S.STAGE_READY)
    draft_id = studio_draft(sconn, piece)
    S.request(sconn, piece.id, S.REQUEST_REVISE, NOTE)
    queue_store.approve(sconn, draft_id)
    status = queue_store.get_draft(sconn, draft_id).status

    assert runner.act_on_requests(bare_context(sconn, cfg)) == 0

    assert stages == []
    after = get(sconn, piece.id)
    assert (after.stage, after.request, after.request_note) == (S.STAGE_READY, "", "")
    assert after.error == (
        f"not revised: draft {draft_id} is {status}, not pending; reopen it in the queue "
        "before revising"
    )


def test_a_revision_of_a_rejected_draft_runs(sconn, cfg, stages):
    """Asking for a revision after rejecting the draft is asking for it back."""
    piece = make_piece(sconn, stage=S.STAGE_READY)
    queue_store.reject(sconn, studio_draft(sconn, piece))
    S.request(sconn, piece.id, S.REQUEST_REVISE, NOTE)
    assert runner.act_on_requests(bare_context(sconn, cfg)) == 1
    assert [call[0] for call in stages] == ["revise"]


def test_requests_are_taken_oldest_first_and_counted(sconn, cfg, stages, clock):
    failed = make_piece(sconn, stage=S.STAGE_FAILED)
    waiting = make_piece(sconn, stage=S.STAGE_RESEARCH_READY)
    ready = make_piece(sconn, stage=S.STAGE_READY)
    studio_draft(sconn, ready)
    misfit = make_piece(sconn, stage=S.STAGE_READY)
    quiet = make_piece(sconn, stage=S.STAGE_RESEARCH_READY)  # nothing asked
    for minutes, (piece, what) in enumerate(
        [
            (waiting, S.REQUEST_CONTINUE),
            (ready, S.REQUEST_REVISE),
            (misfit, S.REQUEST_CONTINUE),
            (failed, S.REQUEST_CONTINUE),
        ],
        start=1,
    ):
        clock.set(T0 + timedelta(minutes=minutes))
        S.request(sconn, piece.id, what, f"note {piece.id}")

    assert runner.act_on_requests(bare_context(sconn, cfg)) == 3
    assert stages == [
        ("write", waiting.id, f"note {waiting.id}", ""),
        ("revise", ready.id, f"note {ready.id}", ""),
        ("resume", failed.id, f"note {failed.id}", ""),
    ]
    assert S.pending_requests(sconn) == []
    assert get(sconn, quiet.id).stage == S.STAGE_RESEARCH_READY
    assert runner.act_on_requests(bare_context(sconn, cfg)) == 0


def test_what_the_editor_changes_while_an_earlier_request_runs_is_what_counts(
    sconn, cfg, monkeypatch, clock
):
    """One run takes the requests in turn, and a stage can run for an hour. A piece the
    editor discards meanwhile (the studio page drops its request) is not written after
    all, and a note the editor rewrote meanwhile is the one its stage gets."""
    first = make_piece(sconn, stage=S.STAGE_RESEARCH_READY)
    gone = make_piece(sconn, stage=S.STAGE_RESEARCH_READY)
    renoted = make_piece(sconn, stage=S.STAGE_FAILED)
    for minutes, piece in enumerate((first, gone, renoted), start=1):
        clock.set(T0 + timedelta(minutes=minutes))
        S.request(sconn, piece.id, S.REQUEST_CONTINUE, "the first note")
    calls: list[tuple[str, int, str]] = []

    def write(ctx: SS.Context, piece: S.Piece, note: str = "", **kw: Any) -> SS.Outcome:
        calls.append(("write", piece.id, note))
        if piece.id == first.id:  # the editor, on the studio page, while this one runs
            S.update_piece(ctx.conn, gone.id, stage=S.STAGE_DISCARDED, request="", request_note="")
            S.request(ctx.conn, renoted.id, S.REQUEST_CONTINUE, "the newer note")
        return SS.Outcome(S.STAGE_READY, "written")

    def resume(ctx: SS.Context, piece: S.Piece, note: str = "", **kw: Any) -> SS.Outcome:
        calls.append(("resume", piece.id, note))
        return SS.Outcome(S.STAGE_READY, "resumed")

    monkeypatch.setattr(SS, "write", write)
    monkeypatch.setattr(SS, "resume_interrupted", resume)
    assert runner.act_on_requests(bare_context(sconn, cfg)) == 2
    assert calls == [
        ("write", first.id, "the first note"),
        ("resume", renoted.id, "the newer note"),
    ]
    assert get(sconn, gone.id).stage == S.STAGE_DISCARDED
    assert S.pending_requests(sconn) == []


def test_continue_writes_the_piece_in_its_own_session_with_the_editors_note(
    rig, sconn, cfg, tmp_path
):
    """Through the real session code and the queue: Continue at the checkpoint resumes the
    session that researched the piece, and the finished piece lands as a pending draft."""
    ws = tmp_path / "studio_pieces" / "piece"
    write_research(ws)
    piece = make_piece(sconn, stage=S.STAGE_RESEARCH_READY, workspace=ws.resolve())
    S.request(sconn, piece.id, S.REQUEST_CONTINUE, NOTE)

    assert runner.act_on_requests(runner.make_context(sconn, cfg)) == 1

    assert rig.cli.stages() == ["write"]  # clean at once: no polish round
    kw = rig.cli.kw("write")
    # the standing instructions go with a resume too (they outlast a compaction that way)
    assert (kw["session_id"], kw["resume"]) == (piece.session_id, True)
    assert kw["system"] == runner.system_text()
    assert Path(kw["cwd"]) == ws.resolve()
    assert f"THE EDITOR READ YOUR FACT BASE AND SAYS\n{NOTE}" in rig.cli.prompt("write")
    after = get(sconn, piece.id)
    assert after.stage == S.STAGE_READY and after.request == ""
    draft = queue_store.find_by_item(sconn, queue_store.studio_item_id(piece.id))
    assert draft is not None and draft.id == after.draft_id
    assert draft.status == queue_store.STATUS_PENDING and draft.draft.thread == [POST]


# ---- new_piece -----------------------------------------------------------------------


@pytest.mark.parametrize(
    "topic, cluster_id, tail",
    [
        ("Merck's KRAS G12D deal", None, "merck-s-kras-g12d-deal"),
        ("", 42, "story-42"),
        ("", None, "auto"),
        ("?!", None, "piece"),
        ("a" * 60, None, "a" * 40),
    ],
)
def test_a_new_piece_gets_its_own_folder_and_a_fresh_session(
    sconn, cfg, tmp_path, topic, cluster_id, tail
):
    piece = runner.new_piece(
        sconn,
        cfg,
        origin=S.ORIGIN_AUTO,
        topic=topic,
        cluster_id=cluster_id,
        checkpoint=False,
    )
    ws = Path(piece.workspace)
    assert ws.is_dir() and ws.parent == (tmp_path / "studio_pieces").resolve()
    assert re.fullmatch(rf"\d{{8}}-\d{{6}}-{re.escape(tail)}", ws.name)
    assert str(uuid.UUID(piece.session_id)) == piece.session_id
    assert (piece.stage, piece.origin, piece.checkpoint) == (
        S.STAGE_RESEARCHING,
        S.ORIGIN_AUTO,
        False,
    )
    assert (piece.topic, piece.cluster_id) == (topic, cluster_id)
    assert (piece.model, piece.effort) == (cfg["model"], cfg["effort"])
    assert piece.story_item == ""  # story 42 is not in the feed: no item to remember


def test_a_new_piece_on_a_feed_story_remembers_one_of_its_items(sconn, cfg):
    story = seed_item(sconn, "s1", total=44)
    seed_item(sconn, "s2", cluster_id=story)
    piece = runner.new_piece(sconn, cfg, origin=S.ORIGIN_MANUAL, cluster_id=story, checkpoint=True)
    assert (piece.cluster_id, piece.story_item) in {(story, "s1"), (story, "s2")}


def test_a_new_piece_keeps_a_known_angle_and_refuses_an_unknown_one(sconn, cfg, tmp_path):
    piece = runner.new_piece(
        sconn, cfg, origin=S.ORIGIN_MANUAL, topic=TOPIC, angle="deal_decoder", checkpoint=True
    )
    assert piece.requested_angle == "deal_decoder" and piece.checkpoint is True
    with pytest.raises(ValueError, match="unknown angle 'no_such_angle'"):
        runner.new_piece(
            sconn, cfg, origin=S.ORIGIN_MANUAL, topic="x", angle="no_such_angle", checkpoint=True
        )
    assert [p.id for p in S.list_pieces(sconn)] == [piece.id]
    assert [p.name for p in (tmp_path / "studio_pieces").iterdir()] == [Path(piece.workspace).name]


# ---- build_brief ---------------------------------------------------------------------


def brief_for(conn, cfg, piece: S.Piece) -> P.Brief:
    return runner.build_brief(
        conn,
        cfg,
        piece,
        library=A.load_angles(),
        playbook="THE PLAYBOOK",
        today="2026-10-05",
        tzname=ZONE,
    )


def test_the_brief_lists_recent_written_pieces_newest_first(sconn, cfg, tmp_path, clock):
    older_ws = tmp_path / "older"
    (older_ws / P.POSTS_DIR).mkdir(parents=True)
    opening = "Merck paid $1.2B upfront for a program its rival walked away from. " * 8
    (older_ws / P.POSTS_DIR / "01.txt").write_text("\ufeff\n" + opening, encoding="utf-8")
    # 03:00 UTC on the 5th is the evening of the 4th in the display zone
    clock.set(datetime(2026, 10, 5, 3, 0, tzinfo=UTC))
    older = make_piece(
        sconn,
        stage=S.STAGE_READY,
        workspace=older_ws,
        title="Merck's walked-away bid",
        angle="deal_decoder",
        shape="long_post",
        hook_style="juxtaposition",
        meta={
            "research": {
                "companies": [{"name": "Merck"}, "Summit", {"ticker": "SMMT"}, {"name": "Akeso"}]
            }
        },
    )
    clock.set(T0)
    newer = make_piece(
        sconn, stage=S.STAGE_FAILED, angle="class_deep_dive", shape="thread", hook_style="question"
    )
    make_piece(sconn, stage=S.STAGE_DISCARDED, angle="catalyst_map")  # given up: not shown
    make_piece(sconn, stage=S.STAGE_RESEARCH_READY)  # not written yet: no angle, not shown
    current = make_piece(sconn, stage=S.STAGE_WRITING, angle="readout_reaction")

    brief = brief_for(sconn, cfg, current)

    assert [r.title for r in brief.recent] == [newer.label, older.label]
    assert older.label == "Merck's walked-away bid"
    first, second = brief.recent
    assert (first.date, first.angle, first.shape, first.hook_style) == (
        "2026-10-05",
        "class_deep_dive",
        "thread",
        "question",
    )
    assert (first.opening, first.companies) == ("", [])  # no posts on disk
    assert second.date == "2026-10-04"  # the display zone's date, as `today` is
    assert second.opening == opening.strip()[:280]  # the BOM and blank line are gone
    assert second.companies == ["Merck", "Akeso"]
    # variety: the recent angles are held back and their hooks named
    assert brief.offer is not None and not brief.offer.forced
    assert brief.offer.held_back == ["class_deep_dive", "deal_decoder"]
    offered = [a.key for a in brief.offer.angles]
    assert "class_deep_dive" not in offered and "deal_decoder" not in offered
    assert "readout_reaction" in offered  # the piece being briefed is not its own history
    assert brief.hooks_to_avoid == ["question", "juxtaposition"]


def test_the_topics_to_avoid_are_every_piece_of_the_window_written_or_not(
    sconn, cfg, tmp_path, clock
):
    """RECENT PIECES lists every piece of the last topics.avoid_days days that was not
    discarded, with how far an unwritten one got: a piece waiting at the checkpoint, or
    stopped, still has its topic. Only written pieces count for variety."""
    cfg["topics"]["avoid_days"] = 10
    clock.set(T0 - timedelta(days=11))
    make_piece(sconn, stage=S.STAGE_READY, angle="scorecard", title="Too old to list")
    clock.set(T0 - timedelta(days=3))
    ready = make_piece(
        sconn,
        stage=S.STAGE_READY,
        angle="deal_decoder",
        title="Merck's walked-away bid",
        draft_id=7,
    )
    clock.set(T0 - timedelta(days=2))
    waiting = make_piece(
        sconn,
        topic="",
        stage=S.STAGE_RESEARCH_READY,
        title="Iovance's TIL relaunch",
        meta={"research": {"companies": [{"name": "Iovance", "ticker": "IOVA"}]}},
    )
    clock.set(T0 - timedelta(days=1))
    stopped = make_piece(sconn, topic="CD19 CAR-T in lupus", stage=S.STAGE_INTERRUPTED)
    failed = make_piece(sconn, topic="Bispecific pricing", stage=S.STAGE_FAILED)
    make_piece(sconn, topic="Given up", stage=S.STAGE_DISCARDED)
    clock.set(T0)
    current = make_piece(sconn, topic="")

    brief = brief_for(sconn, cfg, current)

    assert [(r.title, r.status) for r in brief.topics_to_avoid] == [
        (failed.label, "not written yet: stopped, may be resumed"),
        (stopped.label, "not written yet: stopped, may be resumed"),
        (waiting.label, "not written yet: researched, waiting for the editor"),
        (ready.label, ""),
    ]
    assert brief.topics_to_avoid[2].companies == ["Iovance"]
    assert brief.topics_to_avoid[3].angle == "deal_decoder"
    # variety still comes from the written pieces only, whatever their age
    assert [r.title for r in brief.recent] == [ready.label, "Too old to list"]
    assert brief.offer.held_back == ["deal_decoder", "scorecard"]
    prompt = P.research_prompt(brief)
    assert "Iovance's TIL relaunch · companies: Iovance · not written yet" in prompt
    assert "Too old to list" not in prompt

    cfg["topics"]["avoid_days"] = 0  # lists none
    assert brief_for(sconn, cfg, current).topics_to_avoid == []


def test_the_recent_list_is_as_long_as_the_settings_say(sconn, cfg, clock):
    cfg["variety"].update(recent_pieces_shown=2, avoid_recent_angles=1, avoid_recent_hooks=1)
    for angle, hook in (("deal_decoder", "story"), ("catalyst_map", "contrarian")):
        make_piece(sconn, stage=S.STAGE_READY, angle=angle, hook_style=hook)
    last = make_piece(sconn, stage=S.STAGE_READY, angle="class_deep_dive", hook_style="question")
    brief = brief_for(sconn, cfg, make_piece(sconn, topic="new"))
    assert [r.angle for r in brief.recent] == ["class_deep_dive", "catalyst_map"]
    assert brief.recent[0].title == last.label
    assert brief.offer.held_back == ["class_deep_dive"]
    assert brief.hooks_to_avoid == ["question"]


@pytest.mark.parametrize(
    "research",
    [
        {"companies": None},
        {"companies": "Merck"},
        {"companies": [None, "Merck", {"name": ""}, {"name": None}]},
        {},
        None,
        ["not", "an", "object"],
    ],
)
def test_a_malformed_research_json_never_breaks_a_later_brief(sconn, cfg, research):
    """research.json is the session's own writing (stored as it came in piece.meta); a
    null where the companies list belongs used to fail every later piece's brief."""
    make_piece(sconn, stage=S.STAGE_READY, angle="deal_decoder", meta={"research": research})
    brief = brief_for(sconn, cfg, make_piece(sconn, topic="the next piece"))
    assert [r.companies for r in brief.recent] == [[]]


def test_the_brief_carries_the_paths_the_settings_and_the_checkers_limits(sconn, cfg, tmp_path):
    cfg["x"].update(long_post_max=25000, thread_post_max=20000, headroom=50, max_cards_total=3)
    cfg["x"]["short_post_max"] = 900
    ws = tmp_path / "studio_pieces" / ".." / "studio_pieces" / "piece"
    piece = make_piece(sconn, workspace=ws, requested_angle="")
    brief = brief_for(sconn, cfg, piece)
    assert (brief.piece_id, brief.topic, brief.today, brief.timezone) == (
        piece.id,
        TOPIC,
        "2026-10-05",
        ZONE,
    )
    assert brief.workspace == str(ws.resolve())
    assert brief.reference_dir == str(EXEMPLARS_DIR.resolve())
    assert brief.references == runner.references()
    assert brief.playbook == "THE PLAYBOOK"
    # the limits the checker enforces: X's own less the headroom it keeps
    assert (brief.long_post_max, brief.thread_post_max) == (24950, 19950)
    assert (brief.short_post_max, brief.max_cards) == (900, 3)
    assert brief.recent == [] and brief.hooks_to_avoid == []


def test_a_requested_angle_is_the_only_one_offered(sconn, cfg):
    make_piece(sconn, stage=S.STAGE_READY, angle="deal_decoder")  # used recently, still allowed
    piece = make_piece(sconn, requested_angle="deal_decoder")
    brief = brief_for(sconn, cfg, piece)
    assert brief.offer.forced and [a.key for a in brief.offer.angles] == ["deal_decoder"]


def test_a_piece_from_a_feed_story_gets_that_story_and_no_shortlist(sconn, cfg):
    story = seed_item(sconn, "s1", total=44)
    seed_item(sconn, "s2", total=49)
    piece = make_piece(sconn, topic="", cluster_id=story)
    brief = brief_for(sconn, cfg, piece)
    assert brief.story is not None
    assert (brief.story.cluster_id, brief.story.title, brief.story.score) == (story, "Title s1", 44)
    assert brief.shortlist == []


def test_an_open_piece_is_offered_the_top_stories_no_piece_has_used(sconn, cfg):
    used = seed_item(sconn, "used", total=48)
    queued = seed_item(sconn, "queued", total=46)
    fresh = seed_item(sconn, "fresh", total=40)
    seed_item(sconn, "weak", total=12)  # under topics.min_score
    make_piece(sconn, topic="", cluster_id=used, stage=S.STAGE_DISCARDED)  # even a given-up one
    S.queue_topic(sconn, cluster_id=queued)  # and a story waiting in the queue
    piece = make_piece(sconn, topic="")
    brief = brief_for(sconn, cfg, piece)
    assert brief.story is None
    assert [s.cluster_id for s in brief.shortlist] == [fresh]


def test_an_open_piece_is_not_offered_a_story_the_drafter_already_has(sconn, cfg):
    """One story, one piece of writing: a story with a draft that did not fail (waiting,
    in the A/B pick, approved, rejected) is the drafter's; a failed draft leaves it free."""
    statuses = {
        "pending": queue_store.STATUS_PENDING,
        "choosing": queue_store.STATUS_CHOOSING,
        "approved": queue_store.STATUS_APPROVED,
        "rejected": queue_store.STATUS_REJECTED,
        "failed": queue_store.STATUS_FAILED,
    }
    stories = {name: seed_item(sconn, name, total=45) for name in statuses}
    fresh = seed_item(sconn, "fresh", total=40)
    for name, status in statuses.items():
        queue_store.insert_draft(
            sconn,
            item_id=name,
            cluster_id=stories[name],
            model="m",
            draft=Draft(thread=["x"], suggested_visual="", why_it_matters=""),
            status=status,
        )
    # a drafted story that linking merged into another: the kept cluster is drafted too
    kept = seed_item(sconn, "kept", total=48)
    folded = seed_item(sconn, "folded", total=30)
    queue_store.insert_draft(
        sconn,
        item_id="folded",
        cluster_id=folded,
        model="m",
        draft=Draft(thread=["x"], suggested_visual="", why_it_matters=""),
    )
    merge(kept, folded)

    brief = brief_for(sconn, cfg, make_piece(sconn, topic=""))

    assert sorted(s.cluster_id for s in brief.shortlist) == sorted([stories["failed"], fresh])
    assert "that the account has not written about yet" in P.research_prompt(brief)
    # the radar's scan is given the same stories as leads
    leads = runner.feed_lines(cfg, sconn)
    assert sorted(line.split(" (")[0] for line in leads) == ["Title failed", "Title fresh"]


def test_a_topic_or_a_later_stage_is_never_offered_a_shortlist(sconn, cfg):
    seed_item(sconn, "s1", total=44)
    asked = make_piece(sconn, topic=TOPIC)
    assert brief_for(sconn, cfg, asked).shortlist == []
    writing = make_piece(sconn, topic="", stage=S.STAGE_WRITING)
    assert brief_for(sconn, cfg, writing).shortlist == []


def test_the_references_are_the_folders_with_a_handoff(monkeypatch, tmp_path):
    shipped = runner.references()
    assert {"ctla4_next_gen", "merck_spr2015", "smmt_catalysts"} <= set(shipped)
    assert shipped == sorted(shipped)

    lib = tmp_path / "exemplars"
    for name in ("zeta", "alpha", "no_handoff"):
        (lib / name).mkdir(parents=True)
    (lib / "zeta" / "handoff.md").write_text("z", encoding="utf-8")
    (lib / "alpha" / "handoff.md").write_text("a", encoding="utf-8")
    (lib / "loose.md").write_text("not a folder", encoding="utf-8")
    monkeypatch.setattr(runner, "EXEMPLARS_DIR", lib)
    assert runner.references() == ["alpha", "zeta"]
    monkeypatch.setattr(runner, "EXEMPLARS_DIR", tmp_path / "missing")
    assert runner.references() == []


# ---- make_context and make_renderer ----------------------------------------------------


def test_the_context_reads_the_data_folders_playbook_and_wires_the_queue(rig, sconn, cfg, tmp_path):
    (tmp_path / "studio_playbook.md").write_text("THE EDITOR'S PLAYBOOK\n", encoding="utf-8")
    cfg["x"].update(long_post_max=30000, thread_post_max=25000)
    ctx = runner.make_context(sconn, cfg)
    assert ctx.launch is None  # claude_cli.run_session, looked up at call time
    assert ctx.renderer is rig.renderer and ctx.reference_dir == EXEMPLARS_DIR
    assert ctx.system == runner.system_text() and ctx.system.strip()
    assert ctx.stop_after_research is False
    assert runner.make_context(sconn, cfg, stop_after_research=True).stop_after_research
    ws = tmp_path / "studio_pieces" / "piece"
    write_research(ws)
    write_piece(ws)
    piece = make_piece(sconn, workspace=ws.resolve())
    assert ctx.brief_for(piece).playbook == "THE EDITOR'S PLAYBOOK\n"
    # a finished piece goes into the queue as a long draft at the larger of X's two limits
    report = qa.check_piece(ws, cfg["x"], ctx.known_handles, ctx.renderer)
    assert report.clean, report.problems
    draft = queue_store.get_draft(sconn, ctx.ingest(piece, report))
    assert draft.item_id == queue_store.studio_item_id(piece.id)
    assert (draft.draft.shape, draft.draft.max_chars) == (SHAPE_LONG, 30000)
    assert draft.model == "writer-model (studio)"
    # the queue's own questions before a revision: may it land, and what did the editor
    # change there meanwhile (the revision starts from that)
    piece = S.get_piece(sconn, piece.id)
    assert ctx.revisable(piece) == "" and ctx.queue_edits(piece) is None
    queue_store.edit(sconn, draft.id, thread=["Changed by hand."], approve_after=False)
    edits = ctx.queue_edits(piece)
    assert edits is not None and edits.posts == ["Changed by hand."]


def test_make_renderer_draws_with_the_configured_browser_and_timeout(monkeypatch, cfg):
    cfg["render"] = {"browser": "chromium-custom", "timeout_seconds": 12}
    monkeypatch.setattr(render_mod, "find_browser", lambda configured: f"/opt/{configured}")
    seen: dict[str, Any] = {}

    def render_card(html: Path, png: Path, *, browser: str, timeout: float) -> str:
        seen.update(html=html, png=png, browser=browser, timeout=timeout)
        return "drawn"

    monkeypatch.setattr(render_mod, "render_card", render_card)
    draw = runner.make_renderer(cfg)
    assert draw is not None
    assert draw(Path("card_1.html"), Path("card_1.png")) == "drawn"
    assert seen == {
        "html": Path("card_1.html"),
        "png": Path("card_1.png"),
        "browser": "/opt/chromium-custom",
        "timeout": 12.0,
    }


def test_without_a_browser_there_is_no_renderer(monkeypatch, cfg, caplog):
    def missing(configured: str | None) -> str:
        raise render_mod.RenderError("no Chromium, Chrome or Edge found")

    monkeypatch.setattr(render_mod, "find_browser", missing)
    with caplog.at_level(logging.WARNING, logger="studio.runner"):
        assert runner.make_renderer(cfg) is None
    assert "cards cannot be drawn: no Chromium, Chrome or Edge found" in caplog.text


# ---- run(): what one studio run does ----------------------------------------------------


def test_an_explicit_topic_comes_first_and_stops_at_the_checkpoint(rig, sconn, cfg):
    waiting = S.queue_topic(sconn, topic="a queued topic")
    assert runner.run(topic=TOPIC) == 0

    piece = rig.only_piece()
    assert (piece.origin, piece.topic, piece.cluster_id) == (S.ORIGIN_MANUAL, TOPIC, None)
    assert piece.checkpoint is True  # studio/config.yaml manual.checkpoint
    assert piece.stage == S.STAGE_RESEARCH_READY and piece.title == ""
    ws = Path(piece.workspace)
    assert ws.parent == rig.root.resolve() and ws.name.endswith("-next-gen-ctla-4")
    assert rig.cli.stages() == ["research"]
    kw = rig.cli.kw("research")
    assert (kw["session_id"], kw["resume"], Path(kw["cwd"])) == (piece.session_id, False, ws)
    assert kw["system"] == runner.system_text()
    assert (kw["model"], kw["effort"]) == (cfg["model"], cfg["effort"])
    prompt = rig.cli.prompt("research")
    assert f"The editor asked for a piece on: {TOPIC}" in prompt
    assert f"Your working folder: {ws}" in prompt
    # the queued topic waits for the next run
    assert [t.id for t in S.queued_topics(sconn)] == [waiting]


def test_an_explicit_topic_without_the_checkpoint_goes_into_the_queue(rig, sconn):
    assert runner.run(topic=TOPIC, checkpoint=False) == 0
    piece = rig.only_piece()
    assert rig.cli.stages() == ["research", "write"]
    assert piece.stage == S.STAGE_READY and piece.checkpoint is False
    assert (piece.angle, piece.shape, piece.hook_style) == (
        "class_deep_dive",
        "long_post",
        "hard_number",
    )
    draft = queue_store.get_draft(sconn, piece.draft_id)
    assert draft.item_id == queue_store.studio_item_id(piece.id)
    assert draft.status == queue_store.STATUS_PENDING and draft.draft.thread == [POST]


def test_a_story_from_the_feed_starts_a_piece_on_that_story(rig, sconn):
    story = seed_item(sconn, "s1", total=44)
    assert runner.run(story=story, angle="readout_reaction") == 0
    piece = rig.only_piece()
    assert (piece.topic, piece.cluster_id, piece.requested_angle) == ("", story, "readout_reaction")
    assert piece.origin == S.ORIGIN_MANUAL and Path(piece.workspace).name.endswith(f"story-{story}")
    assert piece.title == TITLE  # research.json's topic names a piece nobody titled
    prompt = rig.cli.prompt("research")
    assert "Start from this story from the account's feeds." in prompt
    assert f"[story {story}] Title s1" in prompt
    assert "The editor chose this angle for the piece:" in prompt


def test_a_queued_topic_comes_next_and_is_claimed_by_its_piece(rig, sconn, cfg):
    cfg["auto"]["enabled"] = False  # a human's topic is not held back by the automatic limits
    story = seed_item(sconn, "s1", total=40)
    first = S.queue_topic(
        sconn, topic="Merck's KRAS G12D deal", cluster_id=story, angle="deal_decoder"
    )
    second = S.queue_topic(sconn, topic="later", checkpoint=False)

    assert runner.run() == 0

    piece = rig.only_piece()
    assert (piece.origin, piece.topic, piece.cluster_id) == (
        S.ORIGIN_MANUAL,
        "Merck's KRAS G12D deal",
        story,
    )
    assert piece.requested_angle == "deal_decoder" and piece.checkpoint is True
    assert piece.stage == S.STAGE_RESEARCH_READY
    assert [t.id for t in S.queued_topics(sconn)] == [second]
    claimed = sconn.execute("SELECT piece_id FROM studio_topics WHERE id = ?", (first,)).fetchone()
    assert claimed[0] == piece.id
    prompt = rig.cli.prompt("research")
    assert "The editor adds: Merck's KRAS G12D deal" in prompt
    assert "- deal_decoder (Deal decoder)" in prompt


def test_the_run_flags_fill_in_what_a_queued_topic_leaves_open(rig, sconn):
    S.queue_topic(sconn, topic=TOPIC, angle="", checkpoint=False)
    assert runner.run(now=True, angle="class_deep_dive", checkpoint=True) == 0
    piece = rig.only_piece()
    # the topic named no angle, so the run's is used; the flag overrides its checkpoint
    assert piece.requested_angle == "class_deep_dive"
    assert piece.checkpoint is True and piece.stage == S.STAGE_RESEARCH_READY


def test_a_queued_topic_whose_angle_left_the_library_starts_without_it(rig, sconn, caplog):
    queued = S.queue_topic(sconn, topic=TOPIC, angle="retired_angle", checkpoint=True)
    with caplog.at_level(logging.WARNING, logger="studio.runner"):
        assert runner.run() == 0
    piece = rig.only_piece()
    assert (piece.topic, piece.requested_angle) == (TOPIC, "")
    assert S.queued_topics(sconn) == []  # claimed, so it never comes back
    assert (
        sconn.execute("SELECT piece_id FROM studio_topics WHERE id = ?", (queued,)).fetchone()[0]
        == piece.id
    )
    assert "'retired_angle', which is no longer in the library" in caplog.text


def test_an_explicit_story_that_left_the_feed_starts_nothing(rig, sconn, caplog):
    with caplog.at_level(logging.ERROR, logger="studio.runner"):
        assert runner.run(story=99999) == 2
    assert rig.pieces() == [] and rig.cli.calls == []
    assert "story 99999 is not in the feed any more" in caplog.text


def test_a_queued_story_that_left_the_feed_falls_back_to_its_words_or_is_dropped(
    rig, sconn, caplog
):
    S.queue_topic(sconn, cluster_id=99998)  # a story alone: nothing left to write about
    with caplog.at_level(logging.WARNING, logger="studio.runner"):
        assert runner.run() == 0
    assert rig.pieces() == [] and S.queued_topics(sconn) == []
    assert "story 99998 is not in the feed any more" in caplog.text

    S.queue_topic(sconn, topic=TOPIC, cluster_id=99997, checkpoint=True)
    assert runner.run() == 0
    piece = rig.only_piece()
    assert (piece.topic, piece.cluster_id) == (TOPIC, None)


def test_a_queued_story_that_linking_merged_is_followed_to_its_new_cluster(rig, sconn):
    """The Feed queued the story while a session ran; the next score run's linking folded
    it into an older cluster before the studio got to it. The run writes it all the same,
    on the cluster that holds it now."""
    older = seed_item(sconn, "older", total=44)
    newer = seed_item(sconn, "newer", total=41)
    # as the Feed's button queues it (tests/test_studio_web.py: the route keeps the item)
    S.queue_topic(sconn, cluster_id=newer, story_item=T.story_item(newer), checkpoint=True)
    merge(older, newer)

    assert runner.run(now=True) == 0

    piece = rig.only_piece()
    assert piece.cluster_id == older
    assert S.queued_topics(sconn) == []  # claimed by the piece
    assert f"[story {older}] Title older" in rig.cli.prompt("research")


def test_a_dropped_queued_story_lets_the_topic_behind_it_start(rig, sconn, caplog):
    S.queue_topic(sconn, cluster_id=99998)  # a story alone, gone with nothing to follow
    behind = S.queue_topic(sconn, topic=TOPIC, checkpoint=True)  # the press was for this
    with caplog.at_level(logging.WARNING, logger="studio.runner"):
        assert runner.run(now=True) == 0
    assert "story 99998 is not in the feed any more" in caplog.text
    piece = rig.only_piece()
    assert piece.topic == TOPIC and S.queued_topics(sconn) == []
    claimed = sconn.execute("SELECT piece_id FROM studio_topics WHERE id = ?", (behind,))
    assert claimed.fetchone()[0] == piece.id


def test_a_run_points_the_studio_at_merged_stories_before_it_offers_any(rig, sconn, cfg):
    cfg["auto"].update(max_new_per_day=3, min_hours_between=0)
    older = seed_item(sconn, "older", total=47)
    newer = seed_item(sconn, "newer", total=45)
    fresh = seed_item(sconn, "fresh", total=40)
    used = make_piece(
        sconn, topic="", cluster_id=newer, story_item="newer", stage=S.STAGE_DISCARDED
    )
    waiting = make_piece(sconn, cluster_id=newer, story_item="newer", stage=S.STAGE_READY)
    merge(older, newer)
    assert S.used_cluster_ids(sconn) == {newer}  # the dead id, until a run follows it

    assert runner.run() == 0

    assert get(sconn, used.id).cluster_id == older and get(sconn, waiting.id).cluster_id == older
    prompt = rig.cli.prompt("research")
    assert f"[story {fresh}]" in prompt and f"[story {older}]" not in prompt


def test_a_dry_run_follows_a_merged_queued_story_without_writing(rig, sconn, capsys):
    older = seed_item(sconn, "older", total=44)
    newer = seed_item(sconn, "newer", total=41)
    S.queue_topic(sconn, cluster_id=newer, story_item="newer")
    merge(older, newer)
    assert runner.run(dry_run=True) == 0
    assert f"[story {older}] Title older" in capsys.readouterr().out
    assert [t.cluster_id for t in S.queued_topics(sconn)] == [newer]  # read-only


def test_two_pieces_on_one_topic_in_the_same_second_get_their_own_folders(rig, sconn, cfg):
    first = runner.new_piece(sconn, cfg, origin=S.ORIGIN_MANUAL, topic=TOPIC, checkpoint=True)
    second = runner.new_piece(sconn, cfg, origin=S.ORIGIN_MANUAL, topic=TOPIC, checkpoint=True)
    third = runner.new_piece(sconn, cfg, origin=S.ORIGIN_MANUAL, topic=TOPIC, checkpoint=True)
    folders = [Path(p.workspace) for p in (first, second, third)]
    assert len(set(folders)) == 3 and all(f.is_dir() for f in folders)
    if folders[0].name[:15] == folders[1].name[:15] == folders[2].name[:15]:  # same second
        assert [f.name for f in folders[1:]] == [folders[0].name + "-2", folders[0].name + "-3"]


def test_a_queued_topics_own_angle_beats_the_runs(rig, sconn):
    S.queue_topic(sconn, topic=TOPIC, angle="deal_decoder", checkpoint=True)
    assert runner.run(now=True, angle="class_deep_dive") == 0
    assert rig.only_piece().requested_angle == "deal_decoder"


def test_now_starts_an_open_piece_whatever_the_limits(rig, sconn, cfg):
    cfg["auto"]["enabled"] = False
    fresh = seed_item(sconn, "fresh", total=41)
    assert runner.run(now=True, angle="catalyst_map") == 0
    piece = rig.only_piece()
    assert (piece.origin, piece.topic, piece.cluster_id) == (S.ORIGIN_MANUAL, "", None)
    assert piece.requested_angle == "catalyst_map" and piece.checkpoint is True
    assert Path(piece.workspace).name.endswith("-auto")
    prompt = rig.cli.prompt("research")
    assert "Choose the topic yourself." in prompt and f"[story {fresh}] Title fresh" in prompt


def test_an_automatic_run_starts_a_piece_when_the_limits_allow(rig, sconn, cfg):
    cfg["auto"].update(max_new_per_day=3, min_hours_between=0)
    used = seed_item(sconn, "used", total=47)
    fresh = seed_item(sconn, "fresh", total=41)
    make_piece(sconn, topic="", cluster_id=used, stage=S.STAGE_DISCARDED)

    assert runner.run() == 0

    piece = next(p for p in rig.pieces() if p.origin == S.ORIGIN_AUTO)
    assert piece.checkpoint is False and piece.requested_angle == ""
    assert piece.stage == S.STAGE_READY and piece.draft_id is not None
    assert rig.cli.stages() == ["research", "write"]
    prompt = rig.cli.prompt("research")
    assert f"[story {fresh}]" in prompt and f"[story {used}]" not in prompt


@pytest.mark.parametrize(
    "limit",
    [
        {"enabled": False},
        {"max_new_per_day": 0},
    ],
)
def test_an_automatic_run_the_limits_refuse_starts_nothing(rig, sconn, cfg, caplog, limit):
    cfg["auto"].update(limit)
    with caplog.at_level(logging.INFO, logger="studio.runner"):
        assert runner.run() == 0
    assert rig.pieces() == [] and rig.cli.calls == []
    assert "no new piece: " in caplog.text


def test_an_automatic_piece_from_earlier_today_holds_back_the_next(rig, sconn):
    make_piece(sconn, origin=S.ORIGIN_AUTO, stage=S.STAGE_READY)
    assert runner.run() == 0
    assert len(rig.pieces()) == 1 and rig.cli.calls == []


def test_resume_only_acts_on_requests_and_starts_nothing(rig, sconn, cfg, tmp_path):
    ws = tmp_path / "studio_pieces" / "waiting"
    write_research(ws)
    waiting = make_piece(sconn, stage=S.STAGE_RESEARCH_READY, workspace=ws.resolve())
    S.request(sconn, waiting.id, S.REQUEST_CONTINUE, NOTE)
    queued = S.queue_topic(sconn, topic="a queued topic")

    assert runner.run(resume_only=True) == 0

    assert rig.cli.stages() == ["write"]
    assert [p.id for p in rig.pieces()] == [waiting.id]
    assert get(sconn, waiting.id).stage == S.STAGE_READY
    assert [t.id for t in S.queued_topics(sconn)] == [queued]


@pytest.mark.parametrize("decide", [queue_store.approve])
def test_a_resumed_revision_is_not_run_once_its_draft_has_left_pending(
    rig, sconn, tmp_path, decide
):
    """A revision was interrupted, then the editor decided the draft in the queue and
    pressed Resume. The context hands the session the queue's own rule, so no session is
    spent on a revision the queue would refuse; the piece says why."""
    ws = tmp_path / "studio_pieces" / "revised"
    write_research(ws)
    write_piece(ws)
    piece = make_piece(
        sconn,
        stage=S.STAGE_INTERRUPTED,
        workspace=ws.resolve(),
        meta={"failed_stage": "revise", "revise_note": NOTE},
    )
    draft_id = studio_draft(sconn, piece)
    decide(sconn, draft_id)
    status = queue_store.get_draft(sconn, draft_id).status
    S.request(sconn, piece.id, S.REQUEST_CONTINUE, "")

    assert runner.run(resume_only=True) == 0

    assert rig.cli.stages() == []
    after = get(sconn, piece.id)
    assert (after.stage, after.request) == (S.STAGE_INTERRUPTED, "")
    assert after.error == (
        f"draft {draft_id} is {status}, not pending; reopen it in the queue before revising"
    )
    assert queue_store.get_draft(sconn, draft_id).draft.thread == [POST]


def test_requests_are_acted_on_before_a_new_piece_starts(rig, sconn, tmp_path):
    ws = tmp_path / "studio_pieces" / "waiting"
    write_research(ws)
    waiting = make_piece(sconn, stage=S.STAGE_RESEARCH_READY, workspace=ws.resolve())
    S.request(sconn, waiting.id, S.REQUEST_CONTINUE, "")
    assert runner.run(topic=TOPIC) == 0
    assert rig.cli.stages() == ["write", "research"]
    assert len(rig.pieces()) == 2


def test_a_run_first_interrupts_what_a_dead_run_left_mid_stage(rig, sconn, cfg):
    cfg["auto"]["enabled"] = False
    stuck = make_piece(sconn, stage=S.STAGE_POLISHING)
    S.start_run(sconn, stuck.id, "polish")
    assert runner.run() == 0
    after = get(sconn, stuck.id)
    assert after.stage == S.STAGE_INTERRUPTED and after.meta["failed_stage"] == "polish"
    assert [r.outcome for r in S.list_runs(sconn, stuck.id)] == ["interrupted"]


def test_a_run_whose_new_piece_fails_exits_1(rig, sconn):
    rig.cli.results["research"] = claude_cli.SessionResult(
        session_id="", ok=False, subtype="error_during_execution", text="Tool permission denied"
    )
    assert runner.run(topic=TOPIC) == 1
    piece = rig.only_piece()
    assert piece.stage == S.STAGE_FAILED
    assert piece.error == "error_during_execution: Tool permission denied"


def test_a_second_run_finds_the_studio_lock_and_does_nothing(rig, sconn):
    stuck = make_piece(sconn, stage=S.STAGE_WRITING)
    S.queue_topic(sconn, topic="a queued topic")
    held = lock.acquire(rig.root / runner.LOCK_NAME, trust_os_lock=True)
    assert held is not None
    try:
        assert runner.run(now=True) == 0
        assert runner.run(topic=TOPIC) == 0
        assert runner.run(dry_run=True) == 0
    finally:
        held.release()
    # nothing touched: the run holding the lock may be writing that piece right now
    assert get(sconn, stuck.id).stage == S.STAGE_WRITING
    assert [p.id for p in rig.pieces()] == [stuck.id]
    assert len(S.queued_topics(sconn)) == 1 and rig.cli.calls == []

    assert runner.run(now=True) == 0  # the lock is free again
    assert get(sconn, stuck.id).stage == S.STAGE_INTERRUPTED
    assert S.queued_topics(sconn) == [] and rig.cli.stages() == ["research"]


def test_the_lock_is_released_even_when_the_run_raises(rig, sconn):
    with pytest.raises(ValueError, match="unknown angle"):
        runner.run(topic=TOPIC, angle="no_such_angle")
    assert rig.pieces() == []
    held = lock.acquire(rig.root / runner.LOCK_NAME, trust_os_lock=True)
    assert held is not None
    held.release()


def test_a_run_waits_a_moment_for_a_page_that_holds_the_lock(rig, sconn):
    """A studio page checking for a stopped run (settle_stopped) holds the lock for as long
    as a few updates take; a run starting then waits for it rather than skipping its turn
    as if another run were in progress."""
    held = lock.acquire(rig.root / runner.LOCK_NAME, trust_os_lock=True)
    assert held is not None
    timer = threading.Timer(0.1, held.release)
    timer.start()
    try:
        assert runner.run(topic=TOPIC) == 0
    finally:
        timer.join()
    assert rig.cli.stages() == ["research"] and len(rig.pieces()) == 1


# ---- settle_stopped: a run that was stopped, seen from the pages ------------------------


def test_with_no_run_alive_a_piece_left_mid_stage_is_interrupted_at_once(rig, sconn):
    """The Stop button killed run_studio.py mid-stage; the lock went with its process. The
    piece is interrupted, its open run closed, without waiting for the next studio run."""
    stuck = make_piece(sconn, stage=S.STAGE_WRITING)
    S.start_run(sconn, stuck.id, "write")
    done = make_piece(sconn, stage=S.STAGE_READY)

    marked = runner.settle_stopped(sconn, load_studio_config())

    assert [p.id for p in marked] == [stuck.id]
    after = get(sconn, stuck.id)
    assert after.stage == S.STAGE_INTERRUPTED and after.meta["failed_stage"] == "write"
    assert after.error == (
        "the write stage stopped before it finished (stopped, timed out or crashed)"
    )
    assert [r.outcome for r in S.list_runs(sconn, stuck.id)] == ["interrupted"]
    assert get(sconn, done.id).stage == S.STAGE_READY
    # the lock is free again for the next run
    again = lock.acquire(rig.root / runner.LOCK_NAME, trust_os_lock=True)
    assert again is not None
    again.release()


def test_while_a_run_holds_the_lock_its_pieces_are_left_alone(rig, sconn):
    stuck = make_piece(sconn, stage=S.STAGE_POLISHING)
    held = lock.acquire(rig.root / runner.LOCK_NAME, trust_os_lock=True)
    assert held is not None
    try:
        assert runner.settle_stopped(sconn, load_studio_config()) == []
    finally:
        held.release()
    assert get(sconn, stuck.id).stage == S.STAGE_POLISHING


def test_with_nothing_mid_stage_the_lock_is_not_even_looked_at(rig, sconn):
    make_piece(sconn, stage=S.STAGE_READY)
    assert runner.settle_stopped(sconn, load_studio_config()) == []
    assert not rig.root.exists()  # no lock file made


# ---- a piece whose folder is gone, inside a run -----------------------------------------


def test_a_piece_whose_folder_is_gone_fails_and_the_run_goes_on(rig, sconn, tmp_path, caplog):
    """Two Continue requests; the first piece's folder was deleted. That piece fails and
    says why, the second is written, and the run ends normally."""
    pieces = []
    for name in ("gone", "here"):
        ws = tmp_path / "studio_pieces" / name
        write_research(ws)
        piece = make_piece(sconn, stage=S.STAGE_RESEARCH_READY, workspace=ws.resolve())
        S.request(sconn, piece.id, S.REQUEST_CONTINUE, NOTE)
        pieces.append(piece)
    gone, here = pieces
    shutil.rmtree(gone.workspace)

    assert runner.run(resume_only=True) == 0

    after = get(sconn, gone.id)
    assert (after.stage, after.request) == (S.STAGE_FAILED, "")
    assert after.error == f"the piece's folder {gone.workspace} is missing" + SS.PUT_BACK
    assert S.list_runs(sconn, gone.id)[-1].finished_at is not None
    assert get(sconn, here.id).stage == S.STAGE_READY
    # and the next run finds nothing to mark: the piece is not left mid-stage
    assert runner.run(resume_only=True) == 0
    assert get(sconn, gone.id).stage == S.STAGE_FAILED


# ---- the date and the playbook, per stage ----------------------------------------------


def test_each_stage_is_told_the_date_and_the_playbook_as_they_are_when_it_starts(
    rig, sconn, cfg, tmp_path, monkeypatch
):
    """One run acts on every request before it starts a piece, hours of sessions that can
    cross midnight; a stage starting after it is told the new date, and a playbook the
    editor saved meanwhile."""
    days = iter([("2026-10-05", ZONE), ("2026-10-06", ZONE)])
    monkeypatch.setattr(runner, "_today", lambda: next(days))
    ctx = runner.make_context(sconn, cfg)
    piece = make_piece(sconn, stage=S.STAGE_WRITING, workspace=tmp_path / "studio_pieces" / "p")
    first = ctx.brief_for(piece)
    (tmp_path / "studio_playbook.md").write_text("SAVED DURING THE RUN\n", encoding="utf-8")
    second = ctx.brief_for(piece)
    assert (first.today, second.today) == ("2026-10-05", "2026-10-06")
    assert first.playbook != second.playbook == "SAVED DURING THE RUN\n"
    assert "Today is 2026-10-06 (America/Los_Angeles)." in P.write_prompt(second)


# ---- what the app knows that the session cannot look up ---------------------------------


def test_every_stage_is_told_the_handles_the_app_has_verified(rig, sconn, monkeypatch):
    """config.yaml gives Immunocore its @Immunocore, though the company's own site links no
    X account: qa accepts the handle without a page, and voice.md lets the session use one
    the app gives it as verified, so research and write are told every one of them."""
    root = {
        "companies": {
            "feeds": [{"key": "imcr", "name": "Immunocore", "x": "Immunocore", "url": ""}]
        },
        "mentions": [
            {"name": "Johnson & Johnson", "handle": "@JNJNews", "aliases": ["J&J", "Janssen"]},
            {"name": "No handle here"},
        ],
    }
    monkeypatch.setattr(runner, "_root_config", lambda: root)
    assert runner.app_handles(root) == [
        ("Immunocore", "Immunocore"),
        ("JNJNews", "Johnson & Johnson, J&J, Janssen"),
    ]
    assert {h.lower() for h, _ in runner.app_handles(root)} == runner.known_handles(root)

    assert runner.run(topic=TOPIC, checkpoint=False) == 0

    for stage in ("research", "write"):
        prompt = rig.cli.prompt(stage)
        assert "X HANDLES THE APP HAS VERIFIED" in prompt, stage
        assert "- @Immunocore = Immunocore\n- @JNJNews = Johnson & Johnson, J&J, Janssen" in prompt


def test_a_new_piece_reads_the_accounts_earlier_pieces_in_its_own_folder(rig, sconn):
    """--restricted keeps a session out of other pieces' folders, and a scorecard grades an
    outcome against the bar the account set in an earlier piece: the app copies the recent
    pieces' text, as the queue holds it, into the new piece's folder, saying which went
    out on X."""
    assert runner.run(topic="the first piece", checkpoint=False) == 0
    first = rig.only_piece()
    assert not (Path(first.workspace) / P.EARLIER_FILE).exists()  # nothing written before it
    assert "EARLIER PIECES" not in rig.cli.prompt("research")
    publish_store.connect().close()  # step 3's tables, as its first run makes them
    publish_store.record_post(
        sconn,
        draft_id=first.draft_id,
        text=POST,
        kind=publish_store.KIND_THREAD,
        position=1,
        slot=None,
        tweet_id="1790000000000000001",
    )
    edited = make_piece(sconn, stage=S.STAGE_READY, angle="deal_decoder", title="Merck's bet")
    queue_store.edit(
        sconn, studio_draft(sconn, edited), thread=["The editor's own words."], approve_after=False
    )
    make_piece(sconn, stage=S.STAGE_READY, angle="the_race", title="Never queued")
    rig.cli.calls.clear()

    assert runner.run(topic=TOPIC, checkpoint=False) == 0

    newest = S.list_pieces(sconn)[0]
    earlier = (Path(newest.workspace) / P.EARLIER_FILE).read_text(encoding="utf-8")
    assert earlier.index("Merck's bet") < earlier.index(TITLE)  # newest first
    assert (
        "· Merck's bet (deal_decoder)\n\nwaiting in the approval queue, not posted\n\n"
        "The editor's own words." in earlier
    )
    assert f"· {TITLE} (class_deep_dive, long_post, hard_number)\n\nposted on X\n\n{POST}" in (
        earlier
    )
    assert "Never queued" not in earlier
    for stage in ("research", "write"):
        assert f"is in {P.EARLIER_FILE} in your working folder" in rig.cli.prompt(stage), stage


# ---- an automatic piece needs a browser to draw its cards with -------------------------


def test_without_a_browser_no_automatic_piece_is_started(rig, sconn, caplog):
    """Its research and writing would be spent before polish stopped it for want of a
    browser, every day. The run fails instead, so the runs page shows why."""
    rig.renderer = None
    with caplog.at_level(logging.ERROR, logger="studio.runner"):
        assert runner.run() == 1
    assert rig.pieces() == [] and rig.cli.calls == []
    assert f"no new piece: {qa.NO_BROWSER}; install Edge, Chrome or Chromium" in caplog.text


def test_without_a_browser_the_editors_requests_and_buttons_still_run(rig, sconn, cfg, tmp_path):
    """Only the automatic piece waits: a Continue is acted on, and --now starts a piece
    (one without cards needs no browser; one with cards stops before polish, resumable)."""
    rig.renderer = None
    cfg["auto"]["min_hours_between"] = 0  # the Continue's piece started just now
    ws = tmp_path / "studio_pieces" / "waiting"
    write_research(ws)
    waiting = make_piece(sconn, stage=S.STAGE_RESEARCH_READY, workspace=ws.resolve())
    S.request(sconn, waiting.id, S.REQUEST_CONTINUE, NOTE)
    assert runner.run() == 1
    assert get(sconn, waiting.id).stage == S.STAGE_READY
    assert runner.run(now=True) == 0
    assert len(rig.pieces()) == 2


def test_a_dry_run_prints_the_first_prompt_and_starts_nothing(rig, sconn, capsys):
    queued = S.queue_topic(sconn, topic="a queued topic")
    assert runner.run(dry_run=True, topic=TOPIC, angle="class_deep_dive") == 0
    out = capsys.readouterr().out
    assert out.startswith("STAGE 1 OF 3: RESEARCH")
    assert "This is piece 0." in out
    assert f"The editor asked for a piece on: {TOPIC}" in out
    assert "The editor chose this angle for the piece:" in out
    assert f"Your working folder: {(rig.root / '(new)').resolve()}" in out
    assert rig.pieces() == [] and rig.cli.calls == []
    assert [t.id for t in S.queued_topics(sconn)] == [queued]
    assert not rig.root.exists()  # read-only: no folder, no lock, nothing marked stale


def test_a_dry_run_shows_the_queued_topic_the_next_run_would_take(rig, sconn, capsys):
    S.queue_topic(sconn, topic="a queued topic", angle="class_deep_dive")
    assert runner.run(dry_run=True) == 0
    out = capsys.readouterr().out
    assert "The editor asked for a piece on: a queued topic" in out
    assert "The editor chose this angle for the piece:" in out
    assert [t.topic for t in S.queued_topics(sconn)] == ["a queued topic"]  # not claimed


def test_a_dry_run_on_a_story_or_on_nothing_shows_what_the_session_would_get(rig, sconn, capsys):
    story = seed_item(sconn, "s1", total=44)
    assert runner.run(dry_run=True, story=story) == 0
    assert f"[story {story}] Title s1" in capsys.readouterr().out
    assert runner.run(dry_run=True) == 0
    out = capsys.readouterr().out
    assert "Choose the topic yourself." in out and f"[story {story}]" in out
    assert rig.pieces() == []


def test_the_list_shows_pieces_newest_first_then_the_queued_topics(rig, sconn, capsys, clock):
    ready = make_piece(
        sconn, stage=S.STAGE_READY, angle="class_deep_dive", title="The class is back", draft_id=7
    )
    clock.set(T0 + timedelta(minutes=5))
    failed = make_piece(sconn, topic="", stage=S.STAGE_FAILED, error="timed out " + "x" * 200)
    words = S.queue_topic(sconn, topic="Merck's KRAS G12D deal")
    story = S.queue_topic(sconn, cluster_id=42)

    assert runner.print_list() == 0

    lines = capsys.readouterr().out.splitlines()
    assert len(lines) == 4
    # times in the display zone, as everything a human reads
    assert lines[0].startswith(f"#{failed.id:<4} 2026-10-05 05:05 PDT  failed ")
    assert f"piece {failed.id}" in lines[0]
    assert lines[0].endswith("!! " + ("timed out " + "x" * 200)[:80])
    assert lines[1].startswith(f"#{ready.id:<4} 2026-10-05 05:00 PDT  ready ")
    assert "class_deep_dive" in lines[1] and lines[1].endswith("The class is back  [draft 7]")
    assert lines[2:] == [
        f"queued topic {words}: Merck's KRAS G12D deal",
        f"queued topic {story}: story 42",
    ]


# ---- run_studio.py ---------------------------------------------------------------------


@pytest.fixture
def cli_calls(monkeypatch) -> dict[str, Any]:
    seen: dict[str, Any] = {}

    def fake_run(**kw: Any) -> int:
        seen["run"] = kw
        return 7

    def fake_list() -> int:
        seen["list"] = True
        return 3

    monkeypatch.setattr(runner, "run", fake_run)
    monkeypatch.setattr(runner, "print_list", fake_list)
    return seen


RUN_DEFAULTS = dict(
    now=False,
    resume_only=False,
    topic="",
    story=None,
    angle="",
    checkpoint=None,
    dry_run=False,
)


@pytest.mark.parametrize(
    "argv, changes",
    [
        ([], {}),
        (["--now"], {"now": True}),
        (["--resume-only"], {"resume_only": True}),
        (["--topic", TOPIC], {"now": True, "topic": TOPIC}),
        (["--story", "123"], {"now": True, "story": 123}),
        (["--now", "--angle", "deal_decoder"], {"now": True, "angle": "deal_decoder"}),
        (["--checkpoint"], {"checkpoint": True}),
        (["--no-checkpoint"], {"checkpoint": False}),
        (["--dry-run"], {"dry_run": True}),
        (
            ["--dry-run", "--topic", TOPIC, "--angle", "deal_decoder", "--no-checkpoint", "-v"],
            {
                "dry_run": True,
                "now": True,
                "topic": TOPIC,
                "angle": "deal_decoder",
                "checkpoint": False,
            },
        ),
    ],
)
def test_main_hands_the_flags_to_the_runner(cli_calls, argv, changes):
    assert run_studio.main(argv) == 7
    assert cli_calls == {"run": {**RUN_DEFAULTS, **changes}}


def test_list_lists_and_runs_nothing(cli_calls):
    assert run_studio.main(["--list", "--now", "--topic", TOPIC]) == 3
    assert cli_calls == {"list": True}


@pytest.mark.parametrize(
    "argv",
    [
        ["--resume"],
        ["--no-check"],
        ["--check"],
        ["--top", TOPIC],
        ["--sto", "12"],
        ["--ang", "deal_decoder"],
        ["--dry"],
        ["--li"],
        ["--no"],
    ],
)
def test_an_abbreviated_flag_is_refused(cli_calls, capsys, argv):
    with pytest.raises(SystemExit) as stop:
        run_studio.main(argv)
    assert stop.value.code == 2
    assert "unrecognized arguments" in capsys.readouterr().err
    assert cli_calls == {}


def test_a_story_must_be_a_number(cli_calls, capsys):
    with pytest.raises(SystemExit) as stop:
        run_studio.main(["--story", "abc"])
    assert stop.value.code == 2
    assert "invalid int value" in capsys.readouterr().err
    assert cli_calls == {}


# ---- topics: the feed stories the studio reads from step 1 -----------------------------


def ids(stories: list[P.Story]) -> list[int]:
    return [s.cluster_id for s in stories]


def age_cluster(conn, cluster_id: int, hours: float) -> None:
    then = (datetime.now(UTC) - timedelta(hours=hours)).isoformat()
    conn.execute(
        "UPDATE clusters SET published_at = ?, created_at = ? WHERE id = ?",
        (then, then, cluster_id),
    )
    conn.commit()


TCFG = {"lookback_hours": 48, "shortlist": 2, "min_score": 30}


def test_the_shortlist_is_the_best_unused_stories_of_the_window(sconn):
    a = seed_item(sconn, "a", total=45)
    b = seed_item(sconn, "b", total=40)
    c = seed_item(sconn, "c", total=35)
    d = seed_item(sconn, "d", total=30)
    weak = seed_item(sconn, "weak", total=29)
    old = seed_item(sconn, "old", total=50)
    age_cluster(sconn, old, hours=49)

    assert ids(T.fetch_shortlist(TCFG, exclude=set())) == [a, b]
    # used stories leave the list and the next best take their places
    assert ids(T.fetch_shortlist(TCFG, exclude={a, b})) == [c, d]
    roomy = {**TCFG, "shortlist": 10}
    assert ids(T.fetch_shortlist(roomy, exclude={b, 999})) == [a, c, d]
    assert ids(T.fetch_shortlist({**roomy, "min_score": 0}, exclude=set())) == [a, b, c, d, weak]
    assert ids(T.fetch_shortlist({**roomy, "lookback_hours": 72}, exclude=set()))[0] == old
    assert T.fetch_shortlist(TCFG, exclude={a, b, c, d}) == []


def test_the_shortlist_defaults_to_eight_stories(sconn):
    made = [seed_item(sconn, f"s{i}", total=40 + i) for i in range(10)]
    assert ids(T.fetch_shortlist({}, exclude=set())) == list(reversed(made))[:8]


def test_a_story_carries_what_the_feed_knows(sconn):
    cid = seed_item(sconn, "p1", source="pubmed", total=30, abstract="A" * 1000)
    seed_item(sconn, "p2", source="biorxiv", cluster_id=cid, abstract="short")
    seed_item(sconn, "p3", source="company_merck", total=42, cluster_id=cid)
    for n, item in enumerate(("p1", "p2", "p3")):
        sconn.execute(
            "UPDATE items SET published_at = ? WHERE id = ?",
            (f"2026-10-0{n + 1}T10:00:00+00:00", item),
        )
    sconn.execute(
        "UPDATE scores SET rationale = ? WHERE cluster_id = ?",
        ("Big   readout\n in a  hot class", cid),
    )
    sconn.execute(
        "UPDATE clusters SET published_at = ? WHERE id = ?", ("2026-10-05T03:00:00+00:00", cid)
    )
    sconn.commit()

    story = T.fetch_story(cid)

    assert story == P.Story(
        cluster_id=cid,
        title="Title p1",
        url=URL,
        source="pubmed (also biorxiv, company_merck)",
        published="2026-10-04",  # the display zone's date
        summary="A" * T.SUMMARY_CHARS,
        score=42,  # the latest score
        why="Big readout in a hot class",
    )


def test_a_story_without_items_or_a_score_still_names_itself(sconn):
    now = datetime.now(UTC).isoformat()
    cur = sconn.execute(
        "INSERT INTO clusters (title, norm_title, published_at, created_at, prefilter_status)"
        " VALUES ('A bare cluster', 'a bare cluster', ?, ?, 'pass')",
        (now, now),
    )
    sconn.commit()
    story = T.fetch_story(cur.lastrowid)
    assert story is not None
    assert (story.title, story.url, story.source, story.summary) == ("A bare cluster", "", "", "")
    assert (story.score, story.why) == (None, "")


def test_a_story_that_is_not_there_is_none(sconn):
    assert T.fetch_story(12345) is None


def test_the_summary_is_the_clusters_longest_abstract(sconn):
    cid = seed_item(sconn, "short", total=40, abstract=ABSTRACT)
    seed_item(sconn, "long", cluster_id=cid, total=40, abstract="B" * 300)
    assert T.fetch_story(cid).summary == "B" * 300


def test_a_story_item_is_the_stories_earliest_item(sconn):
    cid = seed_item(sconn, "late", total=40)
    seed_item(sconn, "early", cluster_id=cid)
    sconn.execute("UPDATE items SET published_at = '2026-10-01T00:00:00+00:00' WHERE id = 'early'")
    sconn.commit()
    assert T.story_item(cid) == "early"
    assert T.story_item(12345) == ""


def test_merged_follows_each_story_item_to_the_cluster_that_holds_it(sconn):
    keep = seed_item(sconn, "keep", total=44)
    folded = seed_item(sconn, "folded", total=41)
    alive = seed_item(sconn, "alive", total=40)
    merge(keep, folded)
    stories = [
        (folded, "folded"),  # merged: followed
        (alive, "alive"),  # still there: nothing to follow
        (keep, "keep"),
        (777, "no-such-item"),  # gone, and its item too
        (778, ""),  # no item remembered
    ]
    assert T.merged(stories) == {folded: keep}
    assert T.merged([]) == {}


def test_no_feed_database_means_no_stories(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "missing" / "pipeline.db"))
    assert T.fetch_shortlist(TCFG, exclude=set()) == []
    assert T.fetch_story(1) is None
    assert T.story_item(1) == "" and T.merged([(1, "x")]) == {}
    assert not (tmp_path / "missing").exists()


def test_an_unreadable_feed_database_means_no_stories(tmp_path, monkeypatch, caplog):
    bad = tmp_path / "pipeline.db"
    bad.write_bytes(b"this is not an SQLite database " * 100)
    monkeypatch.setenv("DB_PATH", str(bad))
    with caplog.at_level(logging.WARNING, logger="studio.topics"):
        assert T.fetch_shortlist(TCFG, exclude=set()) == []
        assert T.fetch_story(1) is None
    assert "no feed stories for the studio" in caplog.text


def test_a_fresh_database_without_scores_offers_nothing(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "fresh.db"))
    assert T.fetch_shortlist(TCFG, exclude=set()) == []
    assert T.fetch_story(1) is None
