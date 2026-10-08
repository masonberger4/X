"""Step 10's stage runner (studio/session.py) end to end, with studio/ingest.py putting the
finished piece into the approval queue.

The Claude Code CLI is faked (`FakeCLI`, passed as Context.launch): it tells the stage from
the first line of its prompt ("STAGE 1 OF 3: RESEARCH", "STAGE 2 OF 3: WRITE", "STAGE 3 OF 3:
POLISH", "REVISION", or the "Your previous run of this stage was interrupted" wrapper around
one of them), writes that stage's files in the piece's folder the way a session would,
appends stream-json events to the transcript as the real launcher does and returns a
SessionResult. The card renderer is faked too (`FakeRenderer`: a PNG header at the 2x card
size), except in the tests at the end, which draw real cards with headless Chromium when a
browser is installed. The DB is a temp file (DB_PATH), opened through
approval_queue/store.py:connect as the studio's runner opens it.
"""

from __future__ import annotations

import functools
import json
import shutil
import sqlite3
import struct
import uuid
import zlib
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

import claude_cli
from approval_queue import store as queue_store
from draft.schema import SHAPE_LONG, Draft
from publish import store as publish_store
from studio import ingest, qa
from studio import prompt as P
from studio import render as render_mod
from studio import session as SS
from studio import store as S
from studio.settings import load_studio_config
from tests.conftest import seed_item

SYSTEM = "STANDING INSTRUCTIONS: who you are, how a piece is made, the voice, the cards."
MODEL = "writer-model"
MAX_CHARS = 25000
TURNS, COST, DURATION = 23, 4.75, 1_800_000

TITLE = "A written-off bispecific is back"
RESEARCH = {
    "topic": "A written-off bispecific's second-line readout",
    "story_id": 42,
    "why_now": "The readout landed this week.",
    "companies": [{"name": "Merck", "ticker": "MRK"}],
    "candidate_angles": [{"angle": "class_deep_dive", "why": "the class is back"}],
    "summary": "A phase 2 readout reopens a class the field had given up on.",
}
FACTBASE = "# Fact base\n\nAs of 2026-10-05: the readout, the class, the competitors.\n"
FACTCHECK = "| post | claim | problem | source | change |\n|---|---|---|---|---|\n"
POST_1 = (
    "A second-line phase 2 readout put a response rate of 41% on the table for a "
    "bispecific most of the field had written off.\n\n"
    "The part that matters is durability: the median response duration was not reached "
    "at 14 months of follow-up. I didn't expect that from this class."
)
POST_2 = (
    "What it changes: the class now has a data point a pivotal trial can be built around, "
    "and @Merck is a year behind with its own program.\n\nNot investment advice."
)
POST_SINGLE = (
    "One post now: the readout, the durability and the year of lead, in a single read. "
    "I keep coming back to the durability.\n\nNot investment advice."
)
ADVICE = "You should buy the stock before the next readout."
SUMMARY = "Two posts on the readout and what it changes for the class."
ANGLE_REASON = "the readout reopens a class the field had given up on"
RECHECK = "the date of the next data update"

CARD_HTML = """<!doctype html>
<html><head><style>
html,body{{margin:0;width:1080px;height:1350px;background:#0b1320;color:#f4f6fb;
font-family:Inter,sans-serif}}
.wrap{{padding:96px 88px}}
h1{{font-size:72px;line-height:1.1;margin:0 0 40px}}
p{{font-size:36px;line-height:1.4;margin:0}}
</style></head>
<body><div class="wrap"><h1>{headline}</h1><p>{line}</p></div>
<i style="position:absolute;right:30px;top:30px;width:2px;height:1290px;background:#334"></i>
</body></html>
"""
# The <i> is a thin rail down the right side: the test card holds one headline and one line,
# and the rail keeps the renderer's empty-band check quiet about the space below them.


@dataclass(frozen=True)
class CardSpec:
    post: int = 1
    alt: str = "Response rate 41% in the second-line phase 2 cohort"
    headline: str = "Response rate 41%"
    line: str = "Phase 2, 97 patients, second line."


CARD_1 = CardSpec()
CARD_2 = CardSpec(
    post=2, alt="The class: who owns each program and how far along it is", headline="The class"
)


# ---- what a session writes ---------------------------------------------------------


def write_research(ws: Path, info: Any = RESEARCH, *, factbase: bool = True) -> None:
    """The research stage's files: factbase.md and research.json (a dict, raw text or None)."""
    if factbase:
        (ws / P.FACTBASE_FILE).write_text(FACTBASE, encoding="utf-8")
    if info is not None:
        text = info if isinstance(info, str) else json.dumps(info)
        (ws / P.RESEARCH_FILE).write_text(text, encoding="utf-8")


def write_piece(
    ws: Path,
    posts: Sequence[str] = (POST_1, POST_2),
    cards: Sequence[CardSpec] = (CARD_1, CARD_2),
    *,
    title: str = TITLE,
    card_html: Callable[[CardSpec], str] | None = None,
) -> None:
    """The write stage's files: posts/NN.txt, cards/card_N.html, factcheck.md, piece.json."""
    (ws / P.POSTS_DIR).mkdir(exist_ok=True)
    (ws / P.CARDS_DIR).mkdir(exist_ok=True)
    post_files = []
    for i, text in enumerate(posts, start=1):
        rel = f"{P.POSTS_DIR}/{i:02d}.txt"
        (ws / rel).write_text(text, encoding="utf-8")
        post_files.append(rel)
    entries = []
    for n, card in enumerate(cards, start=1):
        rel = f"{P.CARDS_DIR}/card_{n}.html"
        page = card_html(card) if card_html else CARD_HTML.format(**card.__dict__)
        (ws / rel).write_text(page, encoding="utf-8")
        entries.append({"file": rel, "post": card.post, "type": "headline_stat", "alt": card.alt})
    (ws / P.FACTCHECK_FILE).write_text(FACTCHECK, encoding="utf-8")
    data = {
        "title": title,
        "angle": "class_deep_dive",
        "angle_reason": ANGLE_REASON,
        "shape": "thread" if len(posts) > 1 else "long_post",
        "hook_style": "hard_number",
        "posts": post_files,
        "cards": entries,
        "companies": [{"name": "Merck", "ticker": "MRK"}],
        "handles": [{"handle": "@Merck", "verified_at": "https://www.merck.com/"}],
        "recheck_before_posting": [RECHECK],
        "summary": SUMMARY,
    }
    (ws / P.PIECE_FILE).write_text(json.dumps(data, indent=2), encoding="utf-8")


# ---- the fake CLI ------------------------------------------------------------------

STAGE_HEADS = (
    ("STAGE 1 OF 3: RESEARCH", "research"),
    ("STAGE 2 OF 3: WRITE", "write"),
    ("STAGE 3 OF 3: POLISH", "polish"),
    ("REVISION", "revise"),
)
RESUME_HEAD = "Your previous run of this stage was interrupted"
FRESH_HEAD = "PICKING UP A PIECE"  # a new session taking over from a lost one
AGAIN = "The stage's instructions, again:"


def stage_of(prompt: str) -> tuple[str, bool]:
    """The stage a prompt asks for, and whether it is the after-an-interruption wrapper
    (on its own, or inside the wrapper of a fresh session that took a piece over)."""
    resumed = prompt.startswith(RESUME_HEAD) or (
        prompt.startswith(FRESH_HEAD) and RESUME_HEAD in prompt
    )
    wrapped = prompt.startswith((RESUME_HEAD, FRESH_HEAD))
    body = prompt.rsplit(AGAIN, 1)[1].lstrip() if wrapped else prompt
    for head, stage in STAGE_HEADS:
        if body.startswith(head):
            return stage, resumed
    raise AssertionError(f"not a stage prompt: {prompt[:80]!r}")


@dataclass
class Call:
    stage: str
    resumed: bool  # the prompt is the "interrupted ... again" wrapper
    prompt: str
    kw: dict[str, Any]
    piece_stage: str  # studio_pieces.stage while the CLI ran
    open_runs: int  # the piece's studio_runs rows still open while the CLI ran


def stopped(subtype: str) -> claude_cli.SessionResult:
    """How a stage that did not finish comes back from claude_cli.run_session."""
    reasons = {
        "error_max_turns": ("max_turns", "Reached the maximum number of turns", ""),
        "killed": ("timed out", "", ""),
        "no_result": ("CLI exited -9", "", "Killed"),
        "error_during_execution": ("", "Tool permission denied", ""),
        "refused": ("refusal", "Usage policy safeguards flagged this request", ""),
        claude_cli.SUBAGENT_KILLED: (
            "the CLI stopped 1 sub-agent(s) before they finished",
            "The cold fact-check is still running in the background.",
            "",
        ),
        "": ("", "", "something went wrong"),
    }
    reason, text, stderr = reasons[subtype]
    return claude_cli.SessionResult(
        session_id="",
        ok=False,
        subtype=subtype,
        text=text,
        terminal_reason=reason,
        num_turns=5,
        cost_usd=0.5,
        duration_ms=60_000,
        returncode=1,
        stderr_tail=stderr,
    )


class FakeCLI:
    """Stands in for claude_cli.run_session (same keyword arguments)."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn
        self.calls: list[Call] = []
        # What each stage writes in the piece's folder when it finishes.
        self.work: dict[str, Callable[[Path, Call], None]] = {
            "research": lambda ws, call: write_research(ws),
            "write": lambda ws, call: write_piece(ws),
        }
        self.outcomes: dict[str, list[Any]] = {}  # stage -> results/exceptions, in turn
        # What each session has cost so far: the CLI reports the session's running total
        # (a resumed session carries on from its last), each finished run adding COST.
        self.spent: dict[str, float] = {}
        self.record_init = True  # False: killed before the CLI recorded the session
        # True: each run ends twice, as a run does when a background sub-agent finishes
        # after the main turn (the CLI starts another turn: a second init and result).
        self.second_turn = False

    def fail(self, stage: str, *outcomes: Any) -> None:
        """The next launches of `stage` return these results (or raise these errors)."""
        self.outcomes.setdefault(stage, []).extend(outcomes)

    def stages(self) -> list[str]:
        return [c.stage for c in self.calls]

    def of(self, stage: str) -> list[Call]:
        return [c for c in self.calls if c.stage == stage]

    def __call__(self, prompt: str, **kw: Any) -> claude_cli.SessionResult:
        stage, resumed = stage_of(prompt)
        ws = Path(kw["cwd"])
        row = self.conn.execute(
            "SELECT id, stage FROM studio_pieces WHERE workspace = ?", (str(ws),)
        ).fetchone()
        open_runs = self.conn.execute(
            "SELECT COUNT(*) FROM studio_runs WHERE piece_id = ? AND finished_at IS NULL",
            (row["id"],),
        ).fetchone()[0]
        call = Call(stage, resumed, prompt, kw, row["stage"], open_runs)
        self.calls.append(call)
        queued = self.outcomes.get(stage) or []
        outcome = queued.pop(0) if queued else None
        if isinstance(outcome, BaseException):
            raise outcome
        if self.record_init:
            self._emit(
                kw,
                {
                    "type": "system",
                    "subtype": "init",
                    "session_id": kw["session_id"],
                    "model": kw["model"],
                    "tools": kw["tools"],
                },
            )
        self._emit(
            kw,
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {
                            "type": "tool_use",
                            "name": "WebSearch",
                            "input": {"query": f"{stage} search"},
                        },
                        {"type": "text", "text": f"Working on the {stage} stage."},
                    ]
                },
            },
        )
        if outcome is not None:  # the stage did not finish
            return outcome
        handler = self.work.get(stage)
        if handler is not None:
            handler(ws, call)
        self._emit(kw, {"type": "result", "subtype": "success", "num_turns": TURNS})
        if self.second_turn:
            self._emit(
                kw,
                {
                    "type": "system",
                    "subtype": "init",
                    "session_id": kw["session_id"],
                    "model": kw["model"],
                    "tools": kw["tools"],
                },
            )
            self._emit(kw, {"type": "result", "subtype": "success", "num_turns": TURNS})
        self.spent[kw["session_id"]] = self.spent.get(kw["session_id"], 0.0) + COST
        return claude_cli.SessionResult(
            session_id=kw["session_id"],
            ok=True,
            subtype="success",
            text=f"{stage} done",
            num_turns=TURNS,
            cost_usd=self.spent[kw["session_id"]],
            duration_ms=DURATION,
            returncode=0,
        )

    @staticmethod
    def _emit(kw: dict[str, Any], event: dict[str, Any]) -> None:
        with open(kw["transcript"], "a", encoding="utf-8") as fh:
            fh.write(json.dumps(event) + "\n")
        kw["on_event"](event)


# ---- the fake renderer -------------------------------------------------------------


def png_bytes(size: tuple[int, int], comment: bytes = b"") -> bytes:
    """A PNG signature, IHDR at `size`, a tEXt chunk and IEND: enough for png_size."""

    def chunk(kind: bytes, data: bytes) -> bytes:
        crc = zlib.crc32(kind + data) & 0xFFFFFFFF
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", crc)

    ihdr = struct.pack(">IIBBBBB", size[0], size[1], 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"tEXt", b"Comment\x00" + comment)
        + chunk(b"IEND", b"")
    )


class FakeRenderer:
    """Stands in for the headless browser: the picture carries its card's HTML (so each
    one is told apart) and layout problems are scripted per card file name."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.problems: dict[str, list[str]] = {}
        self.size = (2160, 2700)

    def __call__(self, html: Path, png: Path) -> render_mod.RenderResult:
        self.calls.append(html.name)
        png.write_bytes(png_bytes(self.size, html.read_bytes()))
        return render_mod.RenderResult(
            png=png, size=render_mod.DEFAULT_SIZE, problems=list(self.problems.get(html.name, []))
        )


# ---- the rig -----------------------------------------------------------------------


class Rig:
    """A Context wired to the fakes, the temp DB and the real queue ingest."""

    def __init__(self, conn: sqlite3.Connection, tmp_path: Path, cfg: dict[str, Any]) -> None:
        self.conn = conn
        self.root = tmp_path / "studio_pieces"
        self.cli = FakeCLI(conn)
        self.renderer = FakeRenderer()
        self.echoed: list[str] = []
        self.briefed: list[S.Piece] = []
        self.shortlist = [P.Story(cluster_id=RESEARCH["story_id"], title="A bispecific readout")]
        self.ctx = SS.Context(
            conn=conn,
            cfg=cfg,
            root_cfg={"models": {"backend": "claude_code"}},
            system=SYSTEM,
            reference_dir=tmp_path / "exemplars",
            known_handles=set(),
            brief_for=self.brief_for,
            renderer=self.renderer,
            ingest=self.ingest,
            launch=self.cli,
            echo=self.echoed.append,
            # as studio/runner.py:make_context wires it
            queue_edits=functools.partial(ingest.hand_edits, conn),
        )

    def brief_for(self, piece: S.Piece) -> P.Brief:
        self.briefed.append(piece)
        # As studio/runner.py:build_brief: a research stage with neither a topic nor a
        # story is offered the feed's shortlist, which holds the story research.json names.
        choosing = not piece.topic and piece.cluster_id is None
        return P.Brief(
            piece_id=piece.id,
            today="2026-10-05",
            timezone="America/Los_Angeles",
            workspace=piece.workspace,
            # the piece's own copy, as studio/runner.py:build_brief names it
            reference_dir=str(Path(piece.workspace) / P.REFERENCE_DIR),
            references=["merck_spr2015"],
            topic=piece.topic,
            shortlist=list(self.shortlist)
            if choosing and piece.stage == S.STAGE_RESEARCHING
            else [],
        )

    def ingest(self, piece: S.Piece, report: qa.Report) -> int:
        return ingest.to_queue(self.conn, piece, report, max_chars=MAX_CHARS)

    def new_piece(
        self,
        *,
        topic: str = "",
        cluster_id: int | None = None,
        checkpoint: bool = False,
        effort: str = "max",
    ) -> S.Piece:
        ws = self.root / f"piece-{uuid.uuid4().hex[:8]}"
        ws.mkdir(parents=True)
        pid = S.create_piece(
            self.conn,
            origin=S.ORIGIN_MANUAL,
            topic=topic,
            cluster_id=cluster_id,
            requested_angle="",
            checkpoint=checkpoint,
            session_id=str(uuid.uuid4()),
            workspace=str(ws.resolve()),
            model=MODEL,
            effort=effort,
        )
        return self.get(pid)

    def ready_piece(self) -> S.Piece:
        """A piece written straight through: in the queue with its posts and cards."""
        piece = self.new_piece()
        out = SS.research(self.ctx, piece)
        assert out.stage == S.STAGE_READY, out.message
        return self.get(piece.id)

    def get(self, piece_id: int) -> S.Piece:
        piece = S.get_piece(self.conn, piece_id)
        assert piece is not None
        return piece

    def draft(self, piece: S.Piece) -> queue_store.DraftRow:
        row = queue_store.find_by_item(self.conn, queue_store.studio_item_id(piece.id))
        assert row is not None, "the piece has no draft in the queue"
        return row

    def no_draft(self, piece: S.Piece) -> bool:
        return queue_store.find_by_item(self.conn, queue_store.studio_item_id(piece.id)) is None

    def log(self, piece: S.Piece) -> str:
        return (Path(piece.workspace) / SS.LOG_FILE).read_text(encoding="utf-8")


@pytest.fixture
def cfg() -> dict[str, Any]:
    c = load_studio_config()
    c["max_polish_rounds"] = 3
    c["timeouts"] = {"research": 150, "write": 180, "polish": 60, "revise": 120}
    c["max_turns"] = {"research": 0, "write": 0, "polish": 40, "revise": 0}
    return c


@pytest.fixture
def rig(conn, tmp_path, cfg) -> Rig:
    S.ensure_tables(conn)
    return Rig(conn, tmp_path, cfg)


def _why(piece_id: int, warnings: Sequence[str] = ()) -> str:
    lines = [
        SUMMARY,
        f"Angle: class_deep_dive ({ANGLE_REASON})",
        f"Re-check before posting: {RECHECK}",
    ]
    if warnings:
        lines.append("For the editor: " + "; ".join(warnings))
    lines.append(f"Fact base, fact-check log and session: /studio/{piece_id}")
    return "\n".join(lines)


# ---- research to the queue ---------------------------------------------------------


def test_a_piece_goes_from_research_to_a_pending_draft_in_the_queue(rig):
    piece = rig.new_piece()
    out = SS.research(rig.ctx, piece)

    assert rig.cli.stages() == ["research", "write", "polish"]
    p = rig.get(piece.id)
    assert out.stage == p.stage == S.STAGE_READY
    assert out.message == f"in the queue as draft {p.draft_id}"
    assert out.report is not None and out.report.clean
    assert p.error == ""
    assert (p.angle, p.shape, p.hook_style, p.title) == (
        "class_deep_dive",
        "thread",
        "hard_number",
        TITLE,
    )
    assert p.cluster_id == 42  # the story the session chose, from research.json
    assert p.meta["research"] == RESEARCH
    assert p.meta["warnings"] == [] and p.meta["problems"] == []

    row = rig.draft(p)
    assert row.id == p.draft_id
    assert row.status == queue_store.STATUS_PENDING
    assert row.item_id == f"studio:{p.id}" and row.studio_piece == p.id
    assert row.cluster_id == 42
    assert row.model == f"{MODEL} (studio)"
    d = row.draft
    assert d.thread == [POST_1, POST_2]
    assert (d.shape, d.max_chars) == (SHAPE_LONG, MAX_CHARS)
    assert d.anchors == [1, 2]
    assert d.wanted_visuals == 2
    assert d.claims_to_verify == []  # the session ran its own cold fact-check
    assert d.chart is None and d.table is None
    assert d.suggested_visual == f"card 1: {CARD_1.alt}; card 2: {CARD_2.alt}"
    assert d.why_it_matters == _why(p.id)
    assert queue_store.list_decisions(rig.conn, row.id) == []  # waiting for the editor


def test_the_cards_are_copied_into_the_queue_and_anchored_to_their_posts(rig):
    piece = rig.ready_piece()
    row = rig.draft(piece)
    cards = Path(piece.workspace) / P.CARDS_DIR

    assert [(i["index"], i["anchor"], i["alt"]) for i in row.images] == [
        (0, 1, CARD_1.alt),
        (1, 2, CARD_2.alt),
    ]
    assert row.image_path == f"draft_{row.id}.png" and row.image_alt == CARD_1.alt
    for k in range(2):
        copied = queue_store.image_file(row.id, k)
        drawn = cards / f"card_{k + 1}.png"
        # A copy (dropping a picture in the queue deletes its file), of the right card.
        assert copied.resolve() != drawn.resolve()
        assert copied.read_bytes() == drawn.read_bytes()
        assert render_mod.png_size(copied) == (2160, 2700)
        assert queue_store.resolve_image(row.images[k]["path"]) == copied
    assert b"The class" in queue_store.image_file(row.id, 1).read_bytes()
    assert not queue_store.image_file(row.id, 2).exists()


def test_every_stage_runs_in_one_session_started_once(rig):
    piece = rig.new_piece()
    SS.research(rig.ctx, piece)

    first, *later = rig.cli.calls
    assert (first.kw["session_id"], first.kw["resume"], first.kw["system"]) == (
        piece.session_id,
        False,
        SYSTEM,
    )
    assert [c.stage for c in later] == ["write", "polish"]
    # A resume carries the standing instructions too: the CLI's record of the first
    # launch's system prompt lasts only until the conversation is compacted.
    for call in later:
        assert (call.kw["session_id"], call.kw["resume"], call.kw["system"]) == (
            piece.session_id,
            True,
            SYSTEM,
        )
    ws = Path(piece.workspace)
    for call in rig.cli.calls:
        assert Path(call.kw["cwd"]) == ws
        assert (call.kw["model"], call.kw["effort"]) == (MODEL, "max")
        assert call.kw["tools"] == call.kw["allowed"] == rig.ctx.cfg["tools"]
        # nothing outside the piece's folder: the reference pieces are copied into it
        assert not call.kw.get("add_dirs")
        assert call.kw["flags"] == rig.ctx.cfg["cli_flags"]
        assert Path(call.kw["transcript"]) == ws / SS.TRANSCRIPT
        assert call.kw["cfg"] is rig.ctx.root_cfg
    limits = {c.stage: (c.kw["max_turns"], c.kw["timeout"]) for c in rig.cli.calls}
    assert limits == {
        "research": (None, 150 * 60.0),
        "write": (None, 180 * 60.0),
        "polish": (40, 60 * 60.0),
    }


# ---- the reference pieces -------------------------------------------------------------


def _ship_references(rig) -> Path:
    """The shipped reference folder, as studio/exemplars holds it: a piece with its handoff
    doc and a card."""
    src = rig.ctx.reference_dir
    (src / "merck_spr2015" / "cards").mkdir(parents=True)
    (src / "merck_spr2015" / "handoff.md").write_text("THE HANDOFF\n", encoding="utf-8")
    (src / "merck_spr2015" / "cards" / "card_1.jpg").write_bytes(b"\xff\xd8 a card")
    return src


def test_each_piece_reads_its_own_copy_of_the_reference_pieces(rig):
    """The session is never handed the shipped reference folder (an --add-dir is a folder
    its Write and Edit can change, and every later piece reads it as the bar): each stage
    finds a copy in the piece's own folder, and what the session writes there stays there."""
    src = _ship_references(rig)

    def research_and_tamper(ws: Path, call: Call) -> None:
        copy = ws / P.REFERENCE_DIR
        assert (copy / "merck_spr2015" / "handoff.md").read_text(encoding="utf-8") == (
            "THE HANDOFF\n"
        )
        assert (copy / "merck_spr2015" / "cards" / "card_1.jpg").read_bytes() == (
            b"\xff\xd8 a card"
        )
        # a web page talked the session into "keeping future pieces consistent"
        (copy / "merck_spr2015" / "handoff.md").write_text("INJECTED\n", encoding="utf-8")
        (copy / "planted").mkdir()
        (copy / "planted" / "handoff.md").write_text("INJECTED\n", encoding="utf-8")
        write_research(ws)

    rig.cli.work["research"] = research_and_tamper
    piece = rig.new_piece()
    SS.research(rig.ctx, piece)

    assert rig.cli.stages() == ["research", "write", "polish"]
    copy = Path(piece.workspace) / P.REFERENCE_DIR
    for call in rig.cli.calls:
        assert not call.kw.get("add_dirs")
        assert str(src) not in call.prompt
    assert f"The reference folder: {copy} " in rig.cli.of("research")[0].prompt
    # the shipped pieces are as they were, and nothing was added to them
    assert (src / "merck_spr2015" / "handoff.md").read_text(encoding="utf-8") == "THE HANDOFF\n"
    assert sorted(p.name for p in src.iterdir()) == ["merck_spr2015"]
    # the copy is the piece's own: made once, not put back over what the session did
    assert (copy / "merck_spr2015" / "handoff.md").read_text(encoding="utf-8") == "INJECTED\n"

    # the next piece starts from the shipped pieces again
    rig.cli.work["research"] = lambda ws, call: write_research(ws)
    other = rig.new_piece(topic="Another topic")
    SS.research(rig.ctx, other)
    fresh = Path(other.workspace) / P.REFERENCE_DIR
    assert (fresh / "merck_spr2015" / "handoff.md").read_text(encoding="utf-8") == "THE HANDOFF\n"
    assert not (fresh / "planted").exists()


def test_a_reference_copy_cut_short_is_made_again(rig):
    src = _ship_references(rig)
    piece = rig.new_piece(checkpoint=True)
    ws = Path(piece.workspace)
    # a stage stopped mid-copy: half a copy beside the folder, never the folder itself
    (ws / f"{P.REFERENCE_DIR}.part" / "merck_spr2015").mkdir(parents=True)

    SS.research(rig.ctx, piece)

    assert not (ws / f"{P.REFERENCE_DIR}.part").exists()
    copied = ws / P.REFERENCE_DIR / "merck_spr2015" / "handoff.md"
    assert copied.read_text(encoding="utf-8") == "THE HANDOFF\n"
    assert (src / "merck_spr2015" / "handoff.md").is_file()


def test_the_reference_copy_never_brings_back_a_folder_that_is_gone(rig):
    _ship_references(rig)
    piece = rig.new_piece(checkpoint=True)
    SS.research(rig.ctx, piece)
    missing = _gone(piece)

    out = SS.write(rig.ctx, rig.get(piece.id))

    assert not Path(piece.workspace).exists()
    assert out.stage == S.STAGE_FAILED and out.message == missing + SS.PUT_BACK


def test_a_piece_without_an_effort_leaves_the_cli_default(rig):
    SS.research(rig.ctx, rig.new_piece(effort=""))
    assert [c.kw["effort"] for c in rig.cli.calls] == [None, None, None]


def test_the_piece_records_its_stage_and_an_open_run_before_each_launch(rig):
    piece = rig.new_piece()
    SS.research(rig.ctx, piece)
    assert [(c.stage, c.piece_stage, c.open_runs) for c in rig.cli.calls] == [
        ("research", S.STAGE_RESEARCHING, 1),
        ("write", S.STAGE_WRITING, 1),
        ("polish", S.STAGE_POLISHING, 1),
    ]


def test_each_launch_is_one_finished_run_row(rig):
    piece = rig.new_piece()
    SS.research(rig.ctx, piece)
    runs = S.list_runs(rig.conn, piece.id)
    assert [(r.stage, r.outcome, r.detail) for r in runs] == [
        ("research", "ok", "finished"),
        ("write", "ok", "finished"),
        ("polish", "ok", "finished"),
    ]
    for r in runs:
        assert r.finished_at is not None
        # each run's own cost, though the CLI reports the session's running total
        assert (r.turns, r.cost_usd, r.duration_ms) == (TURNS, COST, DURATION)


def test_a_run_row_records_what_that_run_cost_not_the_sessions_running_total(rig):
    """The live run's rows read research 11.99 and write 23.52: the CLI's total for the
    whole session so far. Each row is the difference. A run stopped before it reported a
    cost records none, and the next run's total takes it in."""
    totals = iter([11.99, 0.0, 23.52, 34.43])
    real = rig.cli.__call__

    def running_totals(prompt, **kw):
        result = real(prompt, **kw)
        result.cost_usd = next(totals)
        return result

    rig.ctx.launch = running_totals
    rig.cli.fail("write", stopped("killed"))
    piece = rig.new_piece()
    SS.research(rig.ctx, piece)
    SS.resume_interrupted(rig.ctx, rig.get(piece.id))
    runs = S.list_runs(rig.conn, piece.id)
    assert [r.stage for r in runs] == ["research", "write", "write", "polish"]
    assert [round(r.cost_usd, 2) for r in runs] == [11.99, 0.0, 11.53, 10.91]
    assert rig.get(piece.id).meta["session_cost"] == {"session": piece.session_id, "usd": 34.43}


def test_a_fresh_session_counts_its_cost_from_nothing(rig):
    rig.cli.fail("write", stopped("killed"))
    piece = rig.new_piece()
    SS.research(rig.ctx, piece)  # the old session: COST so far
    rig.cli.fail("write", lost(piece.session_id))
    SS.resume_interrupted(rig.ctx, rig.get(piece.id))
    p = rig.get(piece.id)
    runs = S.list_runs(rig.conn, piece.id)
    assert [(r.stage, r.outcome) for r in runs][-2:] == [("write", "ok"), ("polish", "ok")]
    # the new session's first total is all its own, not less the old session's
    assert [r.cost_usd for r in runs][-2:] == [COST, COST]
    assert p.meta["session_cost"] == {"session": p.session_id, "usd": 2 * COST}


def test_the_session_log_and_the_echo_follow_the_session(rig):
    piece = rig.new_piece()
    SS.research(rig.ctx, piece)
    p = rig.get(piece.id)
    lines = rig.log(p).splitlines()

    review = "check: 0 blocking, 0 fixable (card review)"
    ended = f"stage ended: success after {TURNS} turns"
    assert lines[0] == "=== research ==="
    assert f"session started ({MODEL}); tools: {', '.join(rig.ctx.cfg['tools'])}" in lines
    assert "[WebSearch] research search" in lines
    assert "Working on the write stage." in lines
    assert ended in lines
    assert lines.index("=== write ===") < lines.index(review) < lines.index("=== polish ===")
    assert lines[-1] == f"=== ready: draft {p.draft_id} ==="
    assert f"[piece {p.id} research] [WebSearch] research search" in rig.echoed
    assert f"[piece {p.id} polish] {ended}" in rig.echoed
    events = [
        json.loads(line)
        for line in (Path(p.workspace) / SS.TRANSCRIPT).read_text(encoding="utf-8").splitlines()
    ]
    assert sum(e.get("subtype") == "init" for e in events) == 3


def test_a_line_the_console_cannot_show_never_costs_the_log_the_lines_after_it(rig):
    """On Windows a piped stdout is cp1252 and strict, so print raises on an arrow, a >=
    sign or a Greek letter. The session.log must still get that line and every later line
    of the same event (here the tool call after the text), and the stage carries on."""

    def cp1252_console(line: str) -> None:
        line.encode("cp1252")  # what print does on a Windows pipe
        rig.echoed.append(line)

    narration = "ORR ≥ 40% → durable; TGF-β trap next"
    launch = rig.cli

    def narrating(prompt: str, **kw: Any) -> claude_cli.SessionResult:
        text = {"type": "text", "text": narration}
        write = {"type": "tool_use", "name": "Write", "input": {"file_path": "factbase.md"}}
        kw["on_event"]({"type": "assistant", "message": {"content": [text, write]}})
        return launch(prompt, **kw)

    rig.ctx.echo = cp1252_console
    rig.ctx.launch = narrating
    piece = rig.new_piece()

    out = SS.research(rig.ctx, piece)

    assert out.stage == S.STAGE_READY
    lines = rig.log(rig.get(piece.id)).splitlines()
    assert lines.count(narration) == 3  # research, write, polish
    assert lines.count("[Write] factbase.md") == 3
    assert f"[piece {piece.id} research] [Write] factbase.md" in rig.echoed


def test_each_stage_logs_its_start_and_end_once_when_a_sub_agent_adds_a_turn(rig):
    rig.cli.second_turn = True
    piece = rig.new_piece()
    SS.research(rig.ctx, piece)
    lines = rig.log(rig.get(piece.id)).splitlines()
    started = [ln for ln in lines if ln.startswith("session started")]
    ended = [ln for ln in lines if ln.startswith("stage ended")]
    assert len(started) == len(ended) == len(rig.cli.calls) == 3  # research, write, polish
    assert ended == [f"stage ended: success after {TURNS} turns"] * 3


def test_the_brief_is_built_from_the_piece_as_stored(rig):
    piece = rig.new_piece(topic="next-gen CTLA-4")
    SS.research(rig.ctx, piece)
    assert [b.id for b in rig.briefed] == [piece.id, piece.id]  # research, then write
    assert rig.briefed[0].stage == S.STAGE_RESEARCHING
    assert rig.briefed[1].meta["research"] == RESEARCH  # write sees what research found
    assert rig.briefed[1].stage == S.STAGE_WRITING  # so no story shortlist is fetched
    assert "Your working folder: " + piece.workspace in rig.cli.calls[0].prompt


# ---- the research checkpoint -------------------------------------------------------


def test_a_checkpoint_piece_stops_after_research(rig):
    piece = rig.new_piece(checkpoint=True)
    out = SS.research(rig.ctx, piece)

    assert (out.stage, out.message) == (
        S.STAGE_RESEARCH_READY,
        "research done; waiting for the editor",
    )
    assert rig.cli.stages() == ["research"]
    held = rig.get(piece.id)
    assert held.stage == S.STAGE_RESEARCH_READY
    assert held.title == RESEARCH["topic"]  # no topic was asked for: research names it
    assert held.cluster_id == 42
    assert held.meta["research"] == RESEARCH
    assert rig.no_draft(held)


def test_continue_after_the_checkpoint_writes_with_the_editors_note(rig):
    piece = rig.new_piece(checkpoint=True)
    SS.research(rig.ctx, piece)
    note = "Lead with the durability, not the response rate."
    S.request(rig.conn, piece.id, S.REQUEST_CONTINUE, note)

    out = SS.write(rig.ctx, rig.get(piece.id), note)

    assert out.stage == S.STAGE_READY
    assert rig.cli.stages() == ["research", "write", "polish"]
    write = rig.cli.of("write")[0]
    assert not write.resumed
    assert write.prompt.startswith("STAGE 2 OF 3: WRITE")
    assert f"THE EDITOR READ YOUR FACT BASE AND SAYS\n{note}\n" in write.prompt
    assert (write.kw["resume"], write.kw["system"]) == (True, SYSTEM)
    p = rig.get(piece.id)
    assert (p.request, p.request_note) == ("", "")  # the request is used up
    assert rig.draft(p).draft.thread == [POST_1, POST_2]


def test_a_write_without_a_note_says_nothing_about_the_editor(rig):
    piece = rig.new_piece(checkpoint=True)
    SS.research(rig.ctx, piece)
    SS.write(rig.ctx, rig.get(piece.id))
    assert "THE EDITOR READ YOUR FACT BASE" not in rig.cli.of("write")[0].prompt


def test_stop_after_research_holds_a_piece_without_its_own_checkpoint(rig):
    rig.ctx.stop_after_research = True
    piece = rig.new_piece(checkpoint=False)
    assert SS.research(rig.ctx, piece).stage == S.STAGE_RESEARCH_READY
    assert rig.cli.stages() == ["research"]


def test_research_never_overrides_the_editors_topic_or_story(rig):
    piece = rig.new_piece(topic="next-gen CTLA-4", cluster_id=7, checkpoint=True)
    SS.research(rig.ctx, piece)
    p = rig.get(piece.id)
    assert (p.title, p.label) == ("", "next-gen CTLA-4")
    assert p.cluster_id == 7
    assert p.meta["research"]["story_id"] == 42  # kept for the studio page only


@pytest.mark.parametrize(
    ("raw", "kept"),
    [
        (None, {}),  # never written
        ("{not json", {}),
        ("[1, 2]", {}),
        # A story number must be a number, and JSON true (an int to Python) is not one.
        ('{"topic": "", "story_id": "forty-two"}', {"topic": "", "story_id": "forty-two"}),
        ('{"story_id": true}', {"story_id": True}),
        ('{"story_id": null}', {"story_id": None}),
    ],
)
def test_research_json_is_optional_and_only_a_real_story_number_counts(rig, raw, kept):
    rig.cli.work["research"] = lambda ws, call: write_research(ws, raw)
    piece = rig.new_piece(checkpoint=True)

    assert SS.research(rig.ctx, piece).stage == S.STAGE_RESEARCH_READY
    p = rig.get(piece.id)
    assert (p.title, p.cluster_id) == ("", None)
    assert p.meta["research"] == kept


@pytest.mark.parametrize(("story_id", "kept"), [(42, 42), ("42", 42), (" 42 ", 42), (7, None)])
def test_a_story_number_counts_only_when_the_brief_offered_it(rig, story_id, kept):
    # The rig offers story 42 (as build_brief offers the feed's shortlist). A digit string
    # names it as well; a number not on offer, invented or mistyped, ties the piece to
    # nothing: the drafter, the studio's shortlist and the weekly report would all treat
    # that unrelated story as this piece's.
    rig.cli.work["research"] = lambda ws, call: write_research(
        ws, {**RESEARCH, "story_id": story_id}
    )
    piece = rig.new_piece(checkpoint=True)

    SS.research(rig.ctx, piece)

    p = rig.get(piece.id)
    assert p.cluster_id == kept
    assert p.meta["offered_stories"] == [42]
    if kept is None:
        assert "research.json names story 7, not one offered; ignored" in rig.log(p)


def test_a_story_the_session_picks_is_kept_with_one_of_its_items(rig, conn):
    # so the piece still holds its story once linking merges it into another cluster
    story = seed_item(conn, "s1", total=44)
    rig.shortlist = [P.Story(cluster_id=story, title="Title s1")]
    rig.cli.work["research"] = lambda ws, call: write_research(ws, {**RESEARCH, "story_id": story})
    piece = rig.new_piece(checkpoint=True)
    SS.research(rig.ctx, piece)
    p = rig.get(piece.id)
    assert (p.cluster_id, p.story_item) == (story, "s1")


@pytest.mark.parametrize(
    ("status", "refused"), [(queue_store.STATUS_PENDING, True), (queue_store.STATUS_FAILED, False)]
)
def test_a_story_the_drafter_wrote_while_research_ran_is_not_written_twice(
    rig, conn, status, refused
):
    # A draft step already on the story when the offers were made stores its thread during
    # the research. One story, one piece of writing: the piece stops before writing, says
    # why, and a Resume researches another story. A draft that failed holds nothing.
    story, other = seed_item(conn, "s1", total=44), seed_item(conn, "s2", total=40)
    rig.shortlist = [P.Story(cluster_id=story, title="s1"), P.Story(cluster_id=other, title="s2")]

    def research(ws: Path, call: Call) -> None:
        queue_store.insert_draft(
            conn,
            item_id="s1",
            cluster_id=story,
            model="m",
            draft=Draft(["x"], "", ""),
            status=status,
        )
        write_research(ws, {**RESEARCH, "story_id": story})

    rig.cli.work["research"] = research
    piece = rig.new_piece()

    out = SS.research(rig.ctx, piece)

    p = rig.get(piece.id)
    if not refused:
        assert out.stage == S.STAGE_READY and p.cluster_id == story
        return
    assert out.stage == S.STAGE_FAILED and rig.cli.stages() == ["research"]
    assert f"story {story} got a draft from the drafter while this research ran" in out.message
    assert (p.cluster_id, p.meta["failed_stage"]) == (None, "research")
    assert p.meta["research"]["story_id"] == story  # what the session found, kept to read
    assert rig.no_draft(p)

    rig.shortlist = [P.Story(cluster_id=other, title="s2")]
    rig.cli.work["research"] = lambda ws, call: write_research(ws, {**RESEARCH, "story_id": other})
    out = SS.resume_interrupted(rig.ctx, p)

    assert out.stage == S.STAGE_READY, out.message
    assert rig.cli.of("research")[-1].resumed
    assert "got a draft from the drafter" in rig.cli.of("research")[-1].prompt
    assert rig.get(piece.id).cluster_id == other


def test_a_piece_on_a_topic_is_offered_no_story_to_name(rig):
    # A topic piece gets no shortlist, so any story number in research.json is invented.
    piece = rig.new_piece(topic="next-gen CTLA-4", checkpoint=True)
    SS.research(rig.ctx, piece)
    p = rig.get(piece.id)
    assert (p.cluster_id, p.meta["offered_stories"]) == (None, [])


def test_a_resumed_research_keeps_the_stories_offered_the_first_time(rig):
    piece = rig.new_piece(checkpoint=True)
    rig.cli.fail("research", stopped("killed"))
    SS.research(rig.ctx, piece)
    # the next brief offers a newer shortlist; the session may still name the first one
    rig.shortlist = [P.Story(cluster_id=99, title="A newer story")]
    out = SS.resume_interrupted(rig.ctx, rig.get(piece.id))
    assert out.stage == S.STAGE_RESEARCH_READY
    p = rig.get(piece.id)
    assert p.meta["offered_stories"] == [42, 99] and p.cluster_id == 42


def test_a_long_research_topic_is_cut_to_a_title(rig):
    rig.cli.work["research"] = lambda ws, call: write_research(ws, {"topic": "x" * 300})
    piece = rig.new_piece(checkpoint=True)
    SS.research(rig.ctx, piece)
    assert rig.get(piece.id).title == "x" * 200


# ---- research and stage failures ---------------------------------------------------


def test_research_that_writes_no_fact_base_fails(rig):
    rig.cli.work["research"] = lambda ws, call: write_research(ws, factbase=False)
    piece = rig.new_piece()
    out = SS.research(rig.ctx, piece)

    p = rig.get(piece.id)
    assert out.stage == p.stage == S.STAGE_FAILED
    assert out.message == p.error == "the session finished without writing factbase.md"
    assert p.meta["failed_stage"] == "research"
    assert rig.cli.stages() == ["research"]
    [run] = S.list_runs(rig.conn, p.id)
    assert run.outcome == "ok"  # the CLI finished; it is the work that is missing
    assert rig.log(p).splitlines()[-1] == (
        "!!! research: the session finished without writing factbase.md"
    )


@pytest.mark.parametrize("subtype", ["error_max_turns", "killed", "no_result"])
@pytest.mark.parametrize("stage", ["research", "write", "polish"])
def test_a_stage_cut_short_leaves_a_resumable_piece(rig, stage, subtype):
    result = stopped(subtype)
    rig.cli.fail(stage, result)
    piece = rig.new_piece()
    out = SS.research(rig.ctx, piece)

    p = rig.get(piece.id)
    assert out.stage == p.stage == S.STAGE_INTERRUPTED
    assert out.message == p.error == result.detail
    assert p.meta["failed_stage"] == stage
    assert rig.cli.stages()[-1] == stage
    last = S.list_runs(rig.conn, p.id)[-1]
    assert (last.stage, last.outcome, last.detail) == (stage, subtype, result.detail)
    assert last.finished_at is not None
    assert rig.no_draft(p)


@pytest.mark.parametrize("subtype", ["error_during_execution", "refused", ""])
@pytest.mark.parametrize("stage", ["research", "write", "polish"])
def test_any_other_failure_leaves_a_failed_piece(rig, stage, subtype):
    result = stopped(subtype)
    rig.cli.fail(stage, result)
    piece = rig.new_piece()
    out = SS.research(rig.ctx, piece)

    p = rig.get(piece.id)
    assert out.stage == p.stage == S.STAGE_FAILED
    assert p.error == result.detail
    assert p.meta["failed_stage"] == stage
    last = S.list_runs(rig.conn, p.id)[-1]
    assert (last.stage, last.outcome) == (stage, subtype or "error")
    assert rig.no_draft(p)


RUNNING = {
    "research": S.STAGE_RESEARCHING,
    "write": S.STAGE_WRITING,
    "polish": S.STAGE_POLISHING,
    "revise": S.STAGE_REVISING,
}


@pytest.mark.parametrize(
    "error",
    [
        claude_cli.ClaudeCliUnavailable("could not start 'claude': No such file or directory"),
        claude_cli.ClaudeCliError("the CLI returned nothing usable"),
    ],
    ids=["unavailable", "error"],
)
@pytest.mark.parametrize("stage", ["research", "write", "polish"])
def test_a_cli_that_cannot_run_closes_the_run_as_an_error(rig, stage, error):
    rig.cli.fail(stage, error)
    piece = rig.new_piece()

    with pytest.raises(type(error)):
        SS.research(rig.ctx, piece)

    runs = S.list_runs(rig.conn, piece.id)
    assert (runs[-1].stage, runs[-1].outcome, runs[-1].detail) == (stage, "error", str(error))
    assert all(r.finished_at is not None for r in runs)
    # Interrupted at once, so the studio page offers Resume instead of showing a run that
    # is not happening; the error still goes up, so the step shows as failed.
    stopped = rig.get(piece.id)
    assert stopped.stage == S.STAGE_INTERRUPTED
    assert stopped.meta["failed_stage"] == stage
    assert stopped.error == f"the CLI could not run: {error}"
    assert rig.no_draft(piece)


def test_a_revision_the_queue_could_not_take_is_not_started(rig):
    piece = rig.ready_piece()
    ready = rig.get(piece.id)
    asked: list[int] = []

    def revisable(p: S.Piece) -> str:
        asked.append(p.id)
        return "draft 7 is approved, not pending; reopen it in the queue before revising"

    rig.ctx.revisable = revisable
    calls = len(rig.cli.calls)
    out = SS.revise(rig.ctx, ready, "Shorter.")

    assert asked == [piece.id] and len(rig.cli.calls) == calls  # no session was started
    assert out.stage == S.STAGE_READY and out.message.startswith("revision not started: ")
    after = rig.get(piece.id)
    assert after.stage == S.STAGE_READY and (after.request, after.request_note) == ("", "")
    assert after.error.startswith("draft 7 is approved")
    assert "!!! revise not started: draft 7 is approved" in rig.log(after)


def test_a_revision_the_queue_can_take_runs(rig):
    piece = rig.ready_piece()
    rig.ctx.revisable = lambda p: ""
    out = SS.revise(rig.ctx, rig.get(piece.id), "Shorter.")
    assert out.stage == S.STAGE_READY and rig.cli.stages()[-2:] == ["revise", "polish"]


def test_a_cli_error_during_a_revision_leaves_the_draft_as_it_was(rig):
    piece = rig.ready_piece()
    before = rig.draft(piece)
    rig.cli.fail("revise", claude_cli.ClaudeCliUnavailable("could not start 'claude'"))

    with pytest.raises(claude_cli.ClaudeCliUnavailable):
        SS.revise(rig.ctx, rig.get(piece.id), "Shorter.")

    last = S.list_runs(rig.conn, piece.id)[-1]
    assert (last.stage, last.outcome) == ("revise", "error")
    stopped = rig.get(piece.id)
    assert (stopped.stage, stopped.meta["failed_stage"]) == (S.STAGE_INTERRUPTED, "revise")
    assert stopped.meta["revise_note"] == "Shorter."  # Resume asks for the same revision
    assert rig.draft(piece).draft.thread == before.draft.thread


def test_without_a_launcher_the_stage_uses_run_session_looked_up_when_it_runs(rig, monkeypatch):
    rig.ctx.launch = None
    monkeypatch.setattr(claude_cli, "run_session", rig.cli)
    out = SS.research(rig.ctx, rig.new_piece())
    assert out.stage == S.STAGE_READY
    assert rig.cli.stages() == ["research", "write", "polish"]


# ---- polish ------------------------------------------------------------------------


def test_a_clean_piece_with_cards_still_gets_one_look_at_its_cards(rig):
    piece = rig.new_piece()
    SS.research(rig.ctx, piece)

    [polish] = rig.cli.of("polish")
    assert polish.prompt.startswith("STAGE 3 OF 3: POLISH (round 1 of 3)")
    assert "found no problems in its checks" in polish.prompt
    assert "\nPROBLEMS\n" not in polish.prompt
    cards = Path(piece.workspace) / P.CARDS_DIR
    assert f"CARD PICTURES\n- {cards / 'card_1.png'}\n- {cards / 'card_2.png'}\n" in polish.prompt
    # Drawn for the first check, and again after the look.
    assert rig.renderer.calls == ["card_1.html", "card_2.html"] * 2


def test_a_clean_piece_without_cards_goes_straight_to_the_queue(rig):
    rig.cli.work["write"] = lambda ws, call: write_piece(ws, posts=[POST_SINGLE], cards=())
    piece = rig.new_piece()
    out = SS.research(rig.ctx, piece)

    assert out.stage == S.STAGE_READY
    assert rig.cli.stages() == ["research", "write"]  # nothing to look at: no polish round
    assert rig.renderer.calls == []
    row = rig.draft(rig.get(piece.id))
    assert row.draft.thread == [POST_SINGLE]
    assert (row.draft.anchors, row.draft.wanted_visuals) == ([1], 0)
    assert row.draft.suggested_visual == ""
    assert row.images == [] and row.image_path is None
    assert rig.get(piece.id).shape == "long_post"


@pytest.mark.parametrize("rounds", [0, None])
def test_at_least_one_round_runs_whatever_the_setting(rig, rounds):
    rig.ctx.cfg["max_polish_rounds"] = rounds
    piece = rig.new_piece()
    assert SS.research(rig.ctx, piece).stage == S.STAGE_READY
    [polish] = rig.cli.of("polish")
    assert polish.prompt.startswith("STAGE 3 OF 3: POLISH (round 1 of 1)")


def test_a_problem_that_blocks_posting_and_is_never_fixed_fails_the_piece(rig):
    rig.ctx.cfg["max_polish_rounds"] = 2
    rig.cli.work["write"] = lambda ws, call: write_piece(
        ws, posts=[f"{POST_1}\n\n{ADVICE}", POST_2]
    )
    piece = rig.new_piece()
    out = SS.research(rig.ctx, piece)

    p = rig.get(piece.id)
    assert out.stage == p.stage == S.STAGE_FAILED
    assert p.meta["failed_stage"] == "polish"
    assert p.error.startswith("still blocked after polishing: post 1 reads as investment advice")
    assert out.message == p.error
    assert any("investment advice" in x for x in p.meta["problems"])
    assert "warnings" in p.meta
    assert rig.no_draft(p) and p.draft_id is None

    polish = rig.cli.of("polish")
    assert [c.prompt.splitlines()[0] for c in polish] == [
        "STAGE 3 OF 3: POLISH (round 1 of 2)",
        "STAGE 3 OF 3: POLISH (round 2 of 2)",
    ]
    for call in polish:
        assert (
            "\nPROBLEMS\n- post 1 reads as investment advice ('You should buy the stock')"
            in call.prompt
        )
        assert "card_1.png" in call.prompt
    assert "check: 1 blocking, 0 fixable" in rig.log(p)


def test_a_blocking_problem_fixed_in_the_first_round_needs_no_second(rig):
    rig.cli.work["write"] = lambda ws, call: write_piece(
        ws, posts=[f"{POST_1}\n\n{ADVICE}", POST_2]
    )
    rig.cli.work["polish"] = lambda ws, call: write_piece(ws)  # the advice is gone
    piece = rig.new_piece()
    out = SS.research(rig.ctx, piece)

    assert out.stage == S.STAGE_READY
    [polish] = rig.cli.of("polish")  # it showed the cards too, so no extra look is needed
    assert "\nPROBLEMS\n" in polish.prompt and "found no problems" not in polish.prompt
    assert rig.draft(rig.get(piece.id)).draft.thread == [POST_1, POST_2]


def test_a_write_that_leaves_no_piece_file_never_reaches_the_queue(rig):
    rig.ctx.cfg["max_polish_rounds"] = 2
    rig.cli.work["write"] = lambda ws, call: None  # the session wrote nothing
    piece = rig.new_piece()
    out = SS.research(rig.ctx, piece)

    p = rig.get(piece.id)
    assert out.stage == p.stage == S.STAGE_FAILED
    assert "piece.json is missing" in p.error
    polish = rig.cli.of("polish")
    assert len(polish) == 2
    assert "\nPROBLEMS\n- piece.json is missing" in polish[0].prompt
    assert "CARD PICTURES\n- (no cards)\n" in polish[0].prompt
    assert rig.renderer.calls == []
    assert rig.no_draft(p)


def test_the_piece_is_checked_again_after_the_last_round(rig):
    rig.ctx.cfg["max_polish_rounds"] = 1
    # The card review is the only round, and the session makes the piece worse in it.
    rig.cli.work["polish"] = lambda ws, call: write_piece(
        ws, posts=[f"{POST_1}\n\n{ADVICE}", POST_2]
    )
    piece = rig.new_piece()
    out = SS.research(rig.ctx, piece)

    p = rig.get(piece.id)
    assert out.stage == p.stage == S.STAGE_FAILED
    assert "found no problems in its checks" in rig.cli.of("polish")[0].prompt
    assert p.error.startswith("still blocked after polishing: post 1 reads as investment advice")
    assert rig.no_draft(p)


def test_problems_the_session_may_leave_reach_the_queue_as_warnings(rig):
    rig.ctx.cfg["max_polish_rounds"] = 2
    overlap = 'text overlaps other text: "41%" and "Phase 2"'
    rig.renderer.problems["card_1.html"] = [overlap]
    lost = CardSpec(post=3, alt="", headline="The class")  # no alt text, and no post 3
    unverified = f"{POST_1} Watch @NovaCellBio next."
    rig.cli.work["write"] = lambda ws, call: write_piece(
        ws, posts=[unverified, POST_2], cards=(CARD_1, lost)
    )
    piece = rig.new_piece()
    out = SS.research(rig.ctx, piece)

    assert out.stage == S.STAGE_READY
    assert len(rig.cli.of("polish")) == 2  # every round was spent on them
    p = rig.get(piece.id)
    expected = {
        f"unresolved: card card_1.html: {overlap}",
        "unresolved: card card_2.html is attached to post 3, which does not exist",
        "unresolved: card card_2.html has no alt text in piece.json",
    }
    warnings = p.meta["warnings"]
    assert expected <= set(warnings)
    assert any(w.startswith("unresolved: post 1 tags @NovaCellBio") for w in warnings)
    assert p.meta["problems"] == []
    row = rig.draft(p)
    assert row.draft.anchors == [1, 2]  # the card on a post that does not exist goes on the last
    assert row.draft.why_it_matters == _why(p.id, warnings)
    assert [i["alt"] for i in row.images] == [CARD_1.alt, ""]


def test_a_piece_file_without_a_title_keeps_the_title_research_gave_it(rig):
    rig.ctx.cfg["max_polish_rounds"] = 1
    rig.cli.work["write"] = lambda ws, call: write_piece(ws, title="")
    piece = rig.new_piece()

    assert SS.research(rig.ctx, piece).stage == S.STAGE_READY  # a missing title is fixable
    p = rig.get(piece.id)
    assert p.title == RESEARCH["topic"]
    assert "unresolved: piece.json has no title" in p.meta["warnings"]


def test_cards_with_no_browser_to_draw_them_keep_the_piece_out_of_the_queue(rig):
    """A missing browser is the machine's problem: no polish round asks the session to fix
    it (the one fix in its reach would be to drop its cards), the piece stops with its
    cards as written, and Resume finishes it once a browser is there."""
    renderer = rig.ctx.renderer
    rig.ctx.renderer = None
    piece = rig.new_piece()  # the shipped three polish rounds (the cfg fixture)
    out = SS.research(rig.ctx, piece)

    p = rig.get(piece.id)
    assert out.stage == p.stage == S.STAGE_FAILED
    assert p.error == f"{qa.NO_BROWSER}; install one, then Resume"
    assert p.meta["failed_stage"] == "polish" and qa.NO_BROWSER in p.meta["problems"]
    assert rig.cli.stages() == ["research", "write"]  # no polish round was spent on it
    assert sorted(f.name for f in (Path(p.workspace) / P.CARDS_DIR).glob("*.html"))
    assert rig.no_draft(p)

    rig.ctx.renderer = renderer
    assert SS.resume_interrupted(rig.ctx, p).stage == S.STAGE_READY
    assert rig.cli.stages() == ["research", "write", "polish"]  # the card review round
    assert len(rig.draft(p).images) == len(qa.read_piece(Path(p.workspace))[0].cards)


def test_a_card_drawn_at_the_wrong_size_blocks(rig):
    rig.ctx.cfg["max_polish_rounds"] = 1
    rig.renderer.size = (1080, 1350)  # a 1x screenshot
    piece = rig.new_piece()
    out = SS.research(rig.ctx, piece)
    assert out.stage == S.STAGE_FAILED
    assert (
        "card card_1.html came out (1080, 1350) instead of (2160, 2700)" in rig.get(piece.id).error
    )


def test_a_queue_that_refuses_the_piece_fails_it(rig):
    def refuse(piece: S.Piece, report: qa.Report) -> int:
        raise sqlite3.OperationalError("database is locked")

    rig.ctx.ingest = refuse
    piece = rig.new_piece()
    out = SS.research(rig.ctx, piece)

    p = rig.get(piece.id)
    assert out.stage == p.stage == S.STAGE_FAILED
    assert p.error == "could not put the piece in the queue: database is locked"
    assert p.meta["failed_stage"] == "polish"
    assert p.draft_id is None


# ---- resuming ----------------------------------------------------------------------


def test_resume_research_that_never_started_begins_a_new_session(rig):
    rig.cli.record_init = False
    rig.cli.fail("research", stopped("no_result"))
    piece = rig.new_piece(checkpoint=True)
    SS.research(rig.ctx, piece)
    stuck = rig.get(piece.id)
    assert stuck.stage == S.STAGE_INTERRUPTED
    transcript = Path(stuck.workspace) / SS.TRANSCRIPT
    assert not claude_cli.session_started(transcript, stuck.session_id)

    rig.cli.record_init = True
    out = SS.resume_interrupted(rig.ctx, stuck)

    assert out.stage == S.STAGE_RESEARCH_READY
    fresh = rig.get(piece.id)
    assert fresh.session_id != piece.session_id
    uuid.UUID(fresh.session_id)
    again = rig.cli.calls[-1]
    assert (again.kw["session_id"], again.kw["resume"], again.kw["system"]) == (
        fresh.session_id,
        False,
        SYSTEM,
    )
    assert not again.resumed  # nothing to resume: the plain research prompt
    assert again.prompt.startswith("STAGE 1 OF 3: RESEARCH")
    assert fresh.error == ""


def test_resume_research_after_the_session_started_resumes_it(rig):
    killed = stopped("killed")
    rig.cli.fail("research", killed)
    piece = rig.new_piece(checkpoint=True)
    SS.research(rig.ctx, piece)
    stuck = rig.get(piece.id)
    assert claude_cli.session_started(Path(stuck.workspace) / SS.TRANSCRIPT, stuck.session_id)

    out = SS.resume_interrupted(rig.ctx, stuck)

    assert out.stage == S.STAGE_RESEARCH_READY
    again = rig.cli.calls[-1]
    assert again.stage == "research" and again.resumed
    assert again.prompt.startswith(f"{RESUME_HEAD} ({killed.detail}).")
    assert (again.kw["session_id"], again.kw["resume"], again.kw["system"]) == (
        piece.session_id,
        True,
        SYSTEM,
    )
    assert rig.get(piece.id).session_id == piece.session_id


def test_resume_write_sends_the_write_stage_again_with_a_new_note(rig):
    cut = stopped("error_max_turns")
    rig.cli.fail("write", cut)
    piece = rig.new_piece()
    SS.research(rig.ctx, piece)
    stuck = rig.get(piece.id)
    assert stuck.meta["failed_stage"] == "write"

    out = SS.resume_interrupted(rig.ctx, stuck, "Keep it to two posts.")

    assert out.stage == S.STAGE_READY
    assert rig.cli.stages() == ["research", "write", "write", "polish"]
    again = rig.cli.of("write")[-1]
    assert again.resumed and again.kw["resume"] is True
    assert again.prompt.startswith(f"{RESUME_HEAD} ({cut.detail}).")
    assert "THE EDITOR READ YOUR FACT BASE AND SAYS\nKeep it to two posts." in again.prompt


def test_an_interrupted_write_is_resumed_with_the_editors_checkpoint_note(rig):
    piece = rig.new_piece(checkpoint=True)
    SS.research(rig.ctx, piece)
    note = "Lead with the durability, not the response rate."
    rig.cli.fail("write", stopped("killed"))
    SS.write(rig.ctx, rig.get(piece.id), note)
    stuck = rig.get(piece.id)
    assert stuck.stage == S.STAGE_INTERRUPTED

    out = SS.resume_interrupted(rig.ctx, stuck)  # the Resume button, with no new note

    assert out.stage == S.STAGE_READY
    again = rig.cli.of("write")[-1]
    assert again.resumed
    assert f"THE EDITOR READ YOUR FACT BASE AND SAYS\n{note}\n" in again.prompt


def test_the_editors_latest_note_is_the_one_a_resumed_write_gets(rig):
    piece = rig.new_piece(checkpoint=True)
    SS.research(rig.ctx, piece)
    rig.cli.fail("write", stopped("killed"), stopped("killed"))
    SS.write(rig.ctx, rig.get(piece.id), "Lead with the durability.")
    SS.resume_interrupted(rig.ctx, rig.get(piece.id), "Lead with the year of lead instead.")
    assert rig.get(piece.id).stage == S.STAGE_INTERRUPTED

    SS.resume_interrupted(rig.ctx, rig.get(piece.id))

    prompts = [c.prompt for c in rig.cli.of("write")]
    assert len(prompts) == 3
    assert "Lead with the year of lead instead." in prompts[1]
    assert "Lead with the year of lead instead." in prompts[2]
    assert "Lead with the durability." not in prompts[2]


def test_resume_with_no_recorded_stage_writes_again(rig):
    piece = rig.new_piece()
    write_research(Path(piece.workspace))
    S.update_piece(rig.conn, piece.id, stage=S.STAGE_FAILED, error="")

    out = SS.resume_interrupted(rig.ctx, rig.get(piece.id))

    assert out.stage == S.STAGE_READY
    assert rig.cli.stages() == ["write", "polish"]
    assert rig.cli.calls[0].prompt.startswith(f"{RESUME_HEAD} (the run stopped).")


def test_resume_polish_checks_and_polishes_again(rig):
    rig.cli.fail("polish", stopped("killed"))
    piece = rig.new_piece()
    SS.research(rig.ctx, piece)
    stuck = rig.get(piece.id)
    assert stuck.meta["failed_stage"] == "polish"

    out = SS.resume_interrupted(rig.ctx, stuck)

    assert out.stage == S.STAGE_READY
    assert rig.cli.stages() == ["research", "write", "polish", "polish"]
    assert not any(c.resumed for c in rig.cli.calls)
    assert rig.draft(rig.get(piece.id)).draft.thread == [POST_1, POST_2]


def test_resume_polish_with_a_note_revises_with_it(rig):
    rig.cli.fail("polish", stopped("killed"))
    piece = rig.new_piece()
    SS.research(rig.ctx, piece)
    stuck = rig.get(piece.id)

    out = SS.resume_interrupted(rig.ctx, stuck, "Lead with the China data.")

    assert out.stage == S.STAGE_READY
    assert rig.cli.stages()[3:] == ["revise", "polish"]
    assert "Lead with the China data." in rig.cli.of("revise")[-1].prompt


def test_resume_research_carries_the_editors_note(rig):
    rig.cli.fail("research", stopped("killed"))
    piece = rig.new_piece(checkpoint=True)
    SS.research(rig.ctx, piece)

    SS.resume_interrupted(rig.ctx, rig.get(piece.id), "Use the 10-K, not the press release.")

    assert "THE EDITOR ADDS\nUse the 10-K, not the press release." in rig.cli.calls[-1].prompt


def test_resume_after_a_polish_that_stayed_blocked_gets_fresh_rounds(rig):
    rig.ctx.cfg["max_polish_rounds"] = 1
    rig.cli.work["write"] = lambda ws, call: write_piece(
        ws, posts=[f"{POST_1}\n\n{ADVICE}", POST_2]
    )
    piece = rig.new_piece()
    assert SS.research(rig.ctx, piece).stage == S.STAGE_FAILED

    rig.cli.work["polish"] = lambda ws, call: write_piece(ws)
    out = SS.resume_interrupted(rig.ctx, rig.get(piece.id))

    assert out.stage == S.STAGE_READY
    p = rig.get(piece.id)
    assert (p.error, p.meta["problems"]) == ("", [])
    assert rig.draft(p).draft.thread == [POST_1, POST_2]


# ---- revising a finished piece -----------------------------------------------------

ONE_CARD = CardSpec(post=1, alt="Durability: not reached at 14 months", headline="Not reached")


def _rewrite_to_one_post(ws: Path, call: Call) -> None:
    write_piece(ws, posts=[POST_SINGLE], cards=(ONE_CARD,))


def test_revise_rewrites_the_queue_draft_in_place(rig):
    piece = rig.ready_piece()
    before = rig.draft(piece)
    stale = queue_store.image_file(before.id, 1)
    assert stale.exists()
    note = "Make it one long post with only the durability card."
    rig.cli.work["revise"] = _rewrite_to_one_post
    S.request(rig.conn, piece.id, S.REQUEST_REVISE, note)

    out = SS.revise(rig.ctx, rig.get(piece.id), note)

    assert out.stage == S.STAGE_READY
    revise = rig.cli.of("revise")[0]
    assert revise.prompt.startswith("REVISION") and note in revise.prompt
    assert (revise.kw["resume"], revise.kw["system"], revise.piece_stage) == (
        True,
        SYSTEM,
        S.STAGE_REVISING,
    )
    p = rig.get(piece.id)
    assert p.draft_id == before.id
    assert (p.request, p.request_note) == ("", "")
    assert p.meta["revise_note"] == note
    assert (p.shape, p.title) == ("long_post", TITLE)

    row = rig.draft(p)
    assert (row.id, row.status) == (before.id, queue_store.STATUS_PENDING)
    assert row.draft.thread == [POST_SINGLE]
    assert (row.draft.shape, row.draft.max_chars) == (SHAPE_LONG, MAX_CHARS)
    assert (row.draft.anchors, row.draft.wanted_visuals) == ([1], 1)
    assert [(i["index"], i["anchor"], i["alt"]) for i in row.images] == [(0, 1, ONE_CARD.alt)]
    assert row.image_alt == ONE_CARD.alt
    first = queue_store.image_file(row.id, 0)
    assert first.read_bytes() == (Path(p.workspace) / P.CARDS_DIR / "card_1.png").read_bytes()
    assert b"Not reached" in first.read_bytes()
    assert not stale.exists()  # fewer cards: the old second picture is gone

    [logged] = [d for d in queue_store.list_decisions(rig.conn, row.id) if d["action"] == "revise"]
    assert json.loads(logged["original_text"]) == {"thread": [POST_1, POST_2]}
    assert json.loads(logged["edited_text"]) == {"thread": [POST_SINGLE]}
    assert logged["note"] == note  # the editor's words, as the queue's own revise logs them


def test_a_revision_with_more_cards_adds_pictures(rig):
    rig.cli.work["write"] = lambda ws, call: write_piece(ws, posts=[POST_SINGLE], cards=(ONE_CARD,))
    piece = rig.ready_piece()
    rig.cli.work["revise"] = lambda ws, call: write_piece(ws)  # back to two posts, two cards

    assert SS.revise(rig.ctx, rig.get(piece.id), "Split it into two posts.").stage == S.STAGE_READY

    row = rig.draft(rig.get(piece.id))
    assert row.draft.thread == [POST_1, POST_2]
    assert [(i["index"], i["anchor"]) for i in row.images] == [(0, 1), (1, 2)]
    assert queue_store.image_file(row.id, 1).exists()


def test_a_rejected_draft_comes_back_to_pending_with_the_revision(rig):
    piece = rig.ready_piece()
    draft_id = rig.get(piece.id).draft_id
    queue_store.reject(rig.conn, draft_id)
    rig.cli.work["revise"] = _rewrite_to_one_post

    out = SS.revise(rig.ctx, rig.get(piece.id), "Shorter.")

    assert out.stage == S.STAGE_READY
    row = queue_store.get_draft(rig.conn, draft_id)
    assert (row.status, row.draft.thread) == (queue_store.STATUS_PENDING, [POST_SINGLE])
    actions = [d["action"] for d in queue_store.list_decisions(rig.conn, draft_id)]
    assert actions[-3:] == ["reject", "reopen", "revise"]


@pytest.mark.parametrize("decide", [queue_store.approve])
def test_revise_leaves_a_draft_that_was_decided_alone(rig, decide):
    piece = rig.ready_piece()
    draft_id = rig.get(piece.id).draft_id
    decide(rig.conn, draft_id)
    before = queue_store.get_draft(rig.conn, draft_id)
    rig.cli.work["revise"] = _rewrite_to_one_post

    out = SS.revise(rig.ctx, rig.get(piece.id), "Shorter.")

    p = rig.get(piece.id)
    assert out.stage == p.stage == S.STAGE_FAILED
    assert p.meta["failed_stage"] == "polish"
    assert p.error.startswith("could not put the piece in the queue: ")
    assert f"draft {draft_id} is {before.status}, not pending" in p.error
    assert p.draft_id == draft_id
    after = queue_store.get_draft(rig.conn, draft_id)
    assert (after.status, after.draft.thread, after.images) == (
        before.status,
        before.draft.thread,
        before.images,
    )
    assert queue_store.image_file(draft_id, 1).exists()
    assert not [
        d for d in queue_store.list_decisions(rig.conn, draft_id) if d["action"] == "revise"
    ]


def test_revise_blocker_names_an_approved_draft_and_lets_a_rejected_one_through(rig):
    p = rig.get(rig.ready_piece().id)
    assert ingest.revise_blocker(rig.conn, p) == ""  # pending: a revision can replace it
    queue_store.reject(rig.conn, p.draft_id)
    assert ingest.revise_blocker(rig.conn, p) == ""  # rejected: the revision brings it back
    queue_store.approve(rig.conn, p.draft_id)
    assert ingest.revise_blocker(rig.conn, p) == (
        f"draft {p.draft_id} is approved, not pending; reopen it in the queue before revising"
    )


def test_discarding_withdraws_a_pending_draft_only(rig):
    p = rig.get(rig.ready_piece().id)
    assert ingest.withdraw(rig.conn, p) == p.draft_id
    row = queue_store.get_draft(rig.conn, p.draft_id)
    assert row.status == queue_store.STATUS_REJECTED
    assert queue_store.list_decisions(rig.conn, p.draft_id)[-1]["note"] == "discarded in the studio"
    assert ingest.withdraw(rig.conn, p) is None  # not pending any more: left alone
    assert ingest.withdraw(rig.conn, rig.new_piece()) is None  # no draft at all


def test_revise_blocker_lets_a_piece_without_a_draft_through(rig):
    assert ingest.revise_blocker(rig.conn, rig.new_piece()) == ""


def test_with_the_queue_check_a_decided_draft_costs_no_session(rig):
    piece = rig.ready_piece()
    queue_store.approve(rig.conn, rig.get(piece.id).draft_id)
    rig.ctx.revisable = functools.partial(ingest.revise_blocker, rig.conn)
    calls = len(rig.cli.calls)
    out = SS.revise(rig.ctx, rig.get(piece.id), "Shorter.")
    assert len(rig.cli.calls) == calls
    assert out.message.startswith("revision not started: draft ")
    assert rig.get(piece.id).stage == S.STAGE_READY


def test_a_revision_refused_by_the_queue_goes_in_once_the_draft_is_reopened(rig):
    piece = rig.ready_piece()
    draft_id = rig.get(piece.id).draft_id
    queue_store.approve(rig.conn, draft_id)
    rig.cli.work["revise"] = _rewrite_to_one_post
    SS.revise(rig.ctx, rig.get(piece.id), "Shorter.")
    assert rig.get(piece.id).stage == S.STAGE_FAILED

    queue_store.reopen(rig.conn, draft_id)
    calls = len(rig.cli.calls)
    out = SS.resume_interrupted(rig.ctx, rig.get(piece.id))

    assert out.stage == S.STAGE_READY
    assert rig.cli.stages()[calls:] == ["polish"]  # the revised files are only checked again
    row = queue_store.get_draft(rig.conn, draft_id)
    assert (row.status, row.draft.thread) == (queue_store.STATUS_PENDING, [POST_SINGLE])
    [logged] = [
        d for d in queue_store.list_decisions(rig.conn, draft_id) if d["action"] == "revise"
    ]
    assert logged["note"] == "Shorter."


@pytest.mark.parametrize(
    ("subtype", "stage"),
    [("killed", S.STAGE_INTERRUPTED), ("error_during_execution", S.STAGE_FAILED)],
)
def test_a_revision_that_does_not_finish_leaves_the_draft_as_it_was(rig, subtype, stage):
    piece = rig.ready_piece()
    before = rig.draft(piece)
    rig.cli.fail("revise", stopped(subtype))

    out = SS.revise(rig.ctx, rig.get(piece.id), "Cut the history section.")

    p = rig.get(piece.id)
    assert out.stage == p.stage == stage
    assert p.meta["failed_stage"] == "revise"
    assert p.meta["revise_note"] == "Cut the history section."
    after = rig.draft(p)
    assert (after.draft.thread, after.images) == (before.draft.thread, before.images)


def test_resume_revise_uses_the_editors_stored_note(rig):
    piece = rig.ready_piece()
    note = "Cut the history section."
    rig.cli.fail("revise", stopped("killed"))
    SS.revise(rig.ctx, rig.get(piece.id), note)
    rig.cli.work["revise"] = _rewrite_to_one_post

    out = SS.resume_interrupted(rig.ctx, rig.get(piece.id))

    assert out.stage == S.STAGE_READY
    again = rig.cli.of("revise")[-1]
    assert again.prompt.startswith("REVISION") and note in again.prompt
    assert rig.draft(rig.get(piece.id)).draft.thread == [POST_SINGLE]


def test_resume_revise_with_a_new_note_uses_that_one(rig):
    piece = rig.ready_piece()
    rig.cli.fail("revise", stopped("killed"))
    SS.revise(rig.ctx, rig.get(piece.id), "Cut the history section.")
    rig.cli.work["revise"] = _rewrite_to_one_post

    SS.resume_interrupted(rig.ctx, rig.get(piece.id), "One post, one card.")

    again = rig.cli.of("revise")[-1]
    assert "One post, one card." in again.prompt and "history" not in again.prompt
    assert rig.get(piece.id).meta["revise_note"] == "One post, one card."


def test_resume_revise_without_any_note_only_checks_again(rig):
    piece = rig.ready_piece()
    S.update_piece(rig.conn, piece.id, stage=S.STAGE_FAILED, meta={"failed_stage": "revise"})
    calls = len(rig.cli.calls)

    out = SS.resume_interrupted(rig.ctx, rig.get(piece.id))

    assert out.stage == S.STAGE_READY
    assert rig.cli.stages()[calls:] == ["polish"]


# ---- the editor's own changes in the queue ------------------------------------------

POST_1_FIXED = POST_1.replace("41%", "38%")


def _keep_files(seen: dict[str, Any]) -> Callable[[Path, Call], None]:
    """A revision that changes only a card: it records the post files and piece.json as the
    session found them, and leaves the text alone."""

    def work(ws: Path, call: Call) -> None:
        seen["piece"] = json.loads((ws / P.PIECE_FILE).read_text(encoding="utf-8"))
        seen["posts"] = [
            (ws / rel).read_text(encoding="utf-8").strip() for rel in seen["piece"]["posts"]
        ]
        card = ws / P.CARDS_DIR / "card_2.html"
        card.write_text(card.read_text(encoding="utf-8").replace("#0b1320", "#1d2840"), "utf-8")

    return work


def test_a_revision_starts_from_the_editors_hand_edit_and_keeps_it(rig):
    piece = rig.ready_piece()
    draft_id = rig.get(piece.id).draft_id
    # the editor fixes a number by hand in the queue, then asks the studio for something else
    queue_store.edit(rig.conn, draft_id, thread=[POST_1_FIXED, POST_2], approve_after=False)
    seen: dict[str, Any] = {}
    rig.cli.work["revise"] = _keep_files(seen)

    out = SS.revise(rig.ctx, rig.get(piece.id), "Make the second card lighter.")

    assert out.stage == S.STAGE_READY
    # the session found the editor's text in its own files, and was told to keep it
    assert seen["posts"] == [POST_1_FIXED, POST_2]
    assert seen["piece"]["posts"] == ["posts/01.txt", "posts/02.txt"]
    prompt = rig.cli.of("revise")[0].prompt
    assert "Make the second card lighter." in prompt
    assert "THE EDITOR'S OWN CHANGES IN THE QUEUE" in prompt
    assert "changed the text by hand in the approval queue" in prompt
    assert "(posts/01.txt, posts/02.txt)" in prompt and "word for word" in prompt
    assert "dropped these cards" not in prompt
    # the queue keeps the fix, and so does what publish would post
    row = queue_store.get_draft(rig.conn, draft_id)
    assert row.draft.thread == [POST_1_FIXED, POST_2] and len(row.images) == 2
    queue_store.approve(rig.conn, draft_id)
    (approved,) = publish_store.fetch_approved(conn=rig.conn)
    assert approved.thread[0] == POST_1_FIXED and "41%" not in approved.thread[0]
    p = rig.get(piece.id)
    assert p.meta["queued"]["posts"] == [POST_1_FIXED, POST_2]
    assert p.meta["hand_edit"] is None  # landed: the next comparison starts here
    assert "the editor's changes in the queue were written into the piece" in rig.log(p)


def test_a_hand_edit_that_adds_or_removes_posts_is_written_back_whole(rig):
    piece = rig.ready_piece()
    draft_id = rig.get(piece.id).draft_id
    three = [POST_1_FIXED, POST_2, "A third post the editor added."]
    queue_store.edit(rig.conn, draft_id, thread=three, approve_after=False)
    seen: dict[str, Any] = {}
    rig.cli.work["revise"] = _keep_files(seen)
    SS.revise(rig.ctx, rig.get(piece.id), "Tighten the close.")
    assert seen["piece"]["posts"] == ["posts/01.txt", "posts/02.txt", "posts/03.txt"]
    assert seen["posts"] == three
    assert rig.draft(rig.get(piece.id)).draft.thread == three

    queue_store.edit(rig.conn, draft_id, thread=[POST_1_FIXED], approve_after=False)
    SS.revise(rig.ctx, rig.get(piece.id), "One post now.")
    assert seen["piece"]["posts"] == ["posts/01.txt"] and seen["posts"] == [POST_1_FIXED]


def test_a_card_the_editor_dropped_stays_dropped(rig):
    piece = rig.ready_piece()
    draft_id = rig.get(piece.id).draft_id
    queue_store.drop_image(rig.conn, draft_id, index=1)  # the second card goes
    seen: dict[str, Any] = {}
    rig.cli.work["revise"] = _keep_files(seen)

    SS.revise(rig.ctx, rig.get(piece.id), "Shorter.")

    assert [c["file"] for c in seen["piece"]["cards"]] == ["cards/card_1.html"]
    prompt = rig.cli.of("revise")[0].prompt
    dropped = f'cards/card_2.html ("{CARD_2.alt}")'
    assert f"dropped these cards from the post in the approval queue: {dropped}" in prompt
    assert "Leave them out unless the request above asks for them back." in prompt
    assert "changed the text by hand" not in prompt  # only a card was dropped
    row = rig.draft(rig.get(piece.id))
    assert [(i["index"], i["alt"]) for i in row.images] == [(0, CARD_1.alt)]
    assert not queue_store.image_file(row.id, 1).exists()


def test_a_dropped_card_comes_back_when_the_session_puts_it_back(rig):
    # "unless the note asks for it": the session decides, and its piece.json is what lands
    piece = rig.ready_piece()
    queue_store.drop_image(rig.conn, rig.get(piece.id).draft_id, index=1)
    rig.cli.work["revise"] = lambda ws, call: write_piece(ws)  # both cards again
    out = SS.revise(rig.ctx, rig.get(piece.id), "Bring the class card back.")
    assert out.stage == S.STAGE_READY
    assert [i["alt"] for i in rig.draft(rig.get(piece.id)).images] == [CARD_1.alt, CARD_2.alt]


def test_a_resumed_revision_leaves_the_sessions_work_since_the_hand_edit_alone(rig):
    piece = rig.ready_piece()
    draft_id = rig.get(piece.id).draft_id
    queue_store.edit(rig.conn, draft_id, thread=[POST_1_FIXED, POST_2], approve_after=False)
    rig.cli.fail("revise", stopped("killed"))
    SS.revise(rig.ctx, rig.get(piece.id), "Add the durability number to the close.")
    ws = Path(piece.workspace)
    assert (ws / "posts/01.txt").read_text(encoding="utf-8").strip() == POST_1_FIXED
    # the session had started on the editor's text when it was stopped
    worked = POST_1_FIXED + "\n\nDurability is the number to watch."
    (ws / "posts/01.txt").write_text(worked, encoding="utf-8")
    rig.cli.work["revise"] = lambda ws, call: None

    out = SS.resume_interrupted(rig.ctx, rig.get(piece.id))

    assert out.stage == S.STAGE_READY
    assert (ws / "posts/01.txt").read_text(encoding="utf-8") == worked  # not written again
    again = rig.cli.of("revise")[-1].prompt
    assert "THE EDITOR'S OWN CHANGES IN THE QUEUE" in again  # still told
    assert rig.draft(rig.get(piece.id)).draft.thread == [worked, POST_2]


def test_a_newer_hand_edit_on_a_stopped_revision_is_written_again(rig):
    piece = rig.ready_piece()
    draft_id = rig.get(piece.id).draft_id
    queue_store.edit(rig.conn, draft_id, thread=[POST_1_FIXED, POST_2], approve_after=False)
    rig.cli.fail("revise", stopped("killed"))
    SS.revise(rig.ctx, rig.get(piece.id), "Shorter.")
    newer = [POST_1_FIXED.replace("14 months", "15 months"), POST_2]
    queue_store.edit(rig.conn, draft_id, thread=newer, approve_after=False)
    seen: dict[str, Any] = {}
    rig.cli.work["revise"] = _keep_files(seen)

    SS.resume_interrupted(rig.ctx, rig.get(piece.id))

    assert seen["posts"] == newer  # the editor's latest text wins
    assert rig.draft(rig.get(piece.id)).draft.thread == newer


def test_resuming_a_stopped_piece_after_a_hand_edit_goes_through_a_revision(rig):
    piece = rig.ready_piece()
    draft_id = rig.get(piece.id).draft_id
    S.update_piece(rig.conn, piece.id, stage=S.STAGE_FAILED, meta={"failed_stage": "polish"})
    queue_store.edit(rig.conn, draft_id, thread=[POST_1_FIXED, POST_2], approve_after=False)
    seen: dict[str, Any] = {}
    rig.cli.work["revise"] = _keep_files(seen)
    calls = len(rig.cli.calls)

    out = SS.resume_interrupted(rig.ctx, rig.get(piece.id))

    # polishing alone would put the session's text back over the editor's
    assert out.stage == S.STAGE_READY
    assert rig.cli.stages()[calls:] == ["revise", "polish"]
    assert "(nothing beyond keeping the changes below)" in rig.cli.of("revise")[-1].prompt
    assert rig.draft(rig.get(piece.id)).draft.thread == [POST_1_FIXED, POST_2]


def test_the_queue_door_never_reverts_a_hand_edit_the_session_did_not_see(rig):
    piece = rig.ready_piece()
    draft_id = rig.get(piece.id).draft_id
    queue_store.edit(rig.conn, draft_id, thread=[POST_1_FIXED, POST_2], approve_after=False)
    report = qa.check_piece(Path(piece.workspace), rig.ctx.cfg["x"], set(), rig.renderer)

    with pytest.raises(ingest.IngestError, match="changed in the queue"):
        ingest.to_queue(rig.conn, rig.get(piece.id), report, max_chars=MAX_CHARS)

    assert rig.draft(rig.get(piece.id)).draft.thread == [POST_1_FIXED, POST_2]


def test_a_card_that_could_not_be_attached_is_never_read_as_the_editors_hand_edit(rig, monkeypatch):
    # The revision's text replaced the draft's, then the second card could not be copied
    # (a full disk, a picture an image viewer holds open): the piece fails while polishing.
    # The record must say what the queue holds now, or Resume would take the session's own
    # revision for the editor's hand edit, revise again and drop the card it never attached.
    piece = rig.ready_piece()
    draft_id = rig.get(piece.id).draft_id
    locked = {queue_store.image_file(draft_id, 1)}
    real_copy = shutil.copyfile

    def copyfile(src, dst, *a, **kw):
        if Path(dst) in locked:
            raise PermissionError(13, "The process cannot access the file", str(dst))
        return real_copy(src, dst, *a, **kw)

    monkeypatch.setattr(ingest.shutil, "copyfile", copyfile)
    revised = [POST_SINGLE, POST_2]
    rig.cli.work["revise"] = lambda ws, call: write_piece(ws, posts=revised)

    out = SS.revise(rig.ctx, rig.get(piece.id), "Open with the one-line summary.")

    assert out.stage == S.STAGE_FAILED and "could not put the piece in the queue" in out.message
    row = rig.draft(rig.get(piece.id))
    assert row.draft.thread == revised and [i["alt"] for i in row.images] == [CARD_1.alt]
    assert ingest.hand_edits(rig.conn, rig.get(piece.id)) is None

    locked.clear()  # the viewer let go of the picture
    rig.cli.work["revise"] = lambda ws, call: None
    calls = len(rig.cli.calls)
    out = SS.resume_interrupted(rig.ctx, rig.get(piece.id))

    assert out.stage == S.STAGE_READY, out.message
    assert rig.cli.stages()[calls:] == ["polish"]  # no revision of an edit nobody made
    row = rig.draft(rig.get(piece.id))
    assert row.draft.thread == revised
    assert [(i["alt"], i["anchor"]) for i in row.images] == [(CARD_1.alt, 1), (CARD_2.alt, 2)]
    files = json.loads((Path(piece.workspace) / P.PIECE_FILE).read_text(encoding="utf-8"))
    assert [c["file"] for c in files["cards"]] == ["cards/card_1.html", "cards/card_2.html"]


def test_no_hand_edit_means_nothing_is_written_back(rig):
    piece = rig.ready_piece()
    queue_store.approve(rig.conn, rig.get(piece.id).draft_id)  # decisions that change nothing
    queue_store.reopen(rig.conn, rig.get(piece.id).draft_id)
    assert ingest.hand_edits(rig.conn, rig.get(piece.id)) is None
    rig.cli.work["revise"] = _rewrite_to_one_post
    SS.revise(rig.ctx, rig.get(piece.id), "One post.")
    assert "THE EDITOR'S OWN CHANGES" not in rig.cli.of("revise")[0].prompt


def test_a_piece_queued_before_ingests_were_recorded_is_compared_with_its_files(rig):
    piece = rig.ready_piece()
    S.update_piece(rig.conn, piece.id, meta={"queued": None})
    assert ingest.hand_edits(rig.conn, rig.get(piece.id)) is None
    draft_id = rig.get(piece.id).draft_id
    queue_store.edit(rig.conn, draft_id, thread=[POST_1_FIXED, POST_2], approve_after=False)
    edits = ingest.hand_edits(rig.conn, rig.get(piece.id))
    assert edits is not None and edits.posts == [POST_1_FIXED, POST_2] and edits.dropped == []
    # the ready piece's files became its record, kept for the comparisons after a revision
    assert rig.get(piece.id).meta["queued"]["posts"] == [POST_1, POST_2]


def test_a_piece_queued_before_ingests_were_recorded_revises_as_before(rig):
    # No hand edit: the revision rewrites the files, and its result is the session's work,
    # never mistaken for an edit the session did not see.
    piece = rig.ready_piece()
    S.update_piece(rig.conn, piece.id, meta={"queued": None})
    rig.cli.work["revise"] = _rewrite_to_one_post

    out = SS.revise(rig.ctx, rig.get(piece.id), "One post.")

    assert out.stage == S.STAGE_READY, out.message
    assert rig.draft(rig.get(piece.id)).draft.thread == [POST_SINGLE]
    # and a piece without a record that is not ready has nothing to compare with
    S.update_piece(rig.conn, piece.id, stage=S.STAGE_FAILED, meta={"queued": None})
    assert ingest.hand_edits(rig.conn, rig.get(piece.id)) is None


# ---- the rejected-to-pending path asks step 3 first ----------------------------------


def test_a_rejected_draft_revised_back_to_pending_loses_its_old_publishing_order(rig):
    piece = rig.ready_piece()
    draft_id = rig.get(piece.id).draft_id
    queue_store.approve(rig.conn, draft_id)
    publish_store.set_order(rig.conn, [draft_id])  # "Set schedule": first in line
    queue_store.reject(rig.conn, draft_id)
    rig.cli.work["revise"] = _rewrite_to_one_post

    assert SS.revise(rig.ctx, rig.get(piece.id), "Shorter.").stage == S.STAGE_READY

    queue_store.approve(rig.conn, draft_id)
    info = queue_store.publish_states(rig.conn, [draft_id]).get(draft_id)
    assert info is None or info.position is None  # no stale #1 jumps the line


def test_a_draft_already_on_x_is_never_revised_back_to_pending(rig):
    piece = rig.ready_piece()
    draft_id = rig.get(piece.id).draft_id
    queue_store.approve(rig.conn, draft_id)
    publish_store.connect().close()  # step 3's tables, as its first run makes them
    assert publish_store.record_manual(rig.conn, draft_id, [POST_1, POST_2])
    queue_store.reject(rig.conn, draft_id)  # Reject shows for every status
    rig.ctx.revisable = functools.partial(ingest.revise_blocker, rig.conn)
    calls = len(rig.cli.calls)

    blocked = ingest.revise_blocker(rig.conn, rig.get(piece.id))
    out = SS.revise(rig.ctx, rig.get(piece.id), "Shorter.")

    assert blocked.startswith(f"draft {draft_id} is already live on X")
    assert len(rig.cli.calls) == calls  # no session spent on a post that is out
    assert out.message == f"revision not started: {blocked}"
    report = qa.check_piece(Path(piece.workspace), rig.ctx.cfg["x"], set(), rig.renderer)
    with pytest.raises(ingest.IngestError, match="already live on X"):
        ingest.to_queue(rig.conn, rig.get(piece.id), report, max_chars=MAX_CHARS)
    assert queue_store.get_draft(rig.conn, draft_id).status == queue_store.STATUS_REJECTED


# ---- the queue door (studio/ingest.py) ---------------------------------------------


def _report(piece_files: qa.PieceFiles | None) -> qa.Report:
    return qa.Report(piece=piece_files)


def test_build_draft_refuses_a_piece_without_posts(rig):
    piece = rig.new_piece()
    with pytest.raises(ingest.IngestError, match="no posts"):
        ingest.build_draft(piece, _report(None), MAX_CHARS)
    with pytest.raises(ingest.IngestError, match="no posts"):
        ingest.build_draft(piece, _report(qa.PieceFiles()), MAX_CHARS)


def test_a_piece_without_a_summary_is_introduced_by_its_label(rig):
    piece = rig.new_piece(topic="next-gen CTLA-4")
    draft = ingest.build_draft(piece, _report(qa.PieceFiles(posts=[POST_SINGLE])), MAX_CHARS)
    assert (
        draft.why_it_matters
        == f"next-gen CTLA-4\nFact base, fact-check log and session: /studio/{piece.id}"
    )
    assert (draft.anchors, draft.wanted_visuals, draft.max_chars) == ([1], 0, MAX_CHARS)


def test_reingesting_without_a_revision_note_logs_a_plain_revision(rig):
    piece = rig.ready_piece()
    report = qa.check_piece(Path(piece.workspace), rig.ctx.cfg["x"], set(), rig.renderer)

    draft_id = ingest.to_queue(rig.conn, rig.get(piece.id), report, max_chars=MAX_CHARS)

    assert draft_id == rig.get(piece.id).draft_id
    [logged] = [
        d for d in queue_store.list_decisions(rig.conn, draft_id) if d["action"] == "revise"
    ]
    assert logged["note"] == "revised in the studio"


# ---- real cards --------------------------------------------------------------------


def _browser() -> str | None:
    """A browser that can actually draw here (one whose sandbox the machine refuses is as
    good as none), the same gate the render tests use."""
    from tests.test_studio_render import working_browser

    return working_browser()


BROWSER = _browser()
needs_browser = pytest.mark.skipif(
    BROWSER is None, reason="no Chromium-family browser that can draw here"
)


def _real_renderer(html: Path, png: Path) -> render_mod.RenderResult:
    assert BROWSER is not None
    return render_mod.render_card(html, png, browser=BROWSER, timeout=60)


@needs_browser
def test_real_cards_are_drawn_checked_and_copied_into_the_queue(rig):
    rig.ctx.renderer = _real_renderer
    piece = rig.new_piece()
    out = SS.research(rig.ctx, piece)

    p = rig.get(piece.id)
    assert out.stage == S.STAGE_READY, p.error or p.meta
    assert p.meta["warnings"] == []
    row = rig.draft(p)
    for k in range(2):
        assert render_mod.png_size(queue_store.image_file(row.id, k)) == (2160, 2700)


@needs_browser
def test_a_layout_problem_the_browser_finds_goes_back_to_the_session(rig):
    rig.ctx.renderer = _real_renderer

    def off_the_edge(card: CardSpec) -> str:
        return CARD_HTML.format(**card.__dict__).replace(
            "<h1>", '<h1 style="position:absolute;left:900px;top:4px;white-space:nowrap">'
        )

    rig.cli.work["write"] = lambda ws, call: write_piece(
        ws, cards=(CARD_1,), card_html=off_the_edge
    )
    rig.cli.work["polish"] = lambda ws, call: write_piece(ws, cards=(CARD_1,))
    piece = rig.new_piece()
    out = SS.research(rig.ctx, piece)

    assert out.stage == S.STAGE_READY
    [polish] = rig.cli.of("polish")
    assert "text runs off the card or within 20px of its edge" in polish.prompt
    assert "card card_1.html: " in polish.prompt
    assert rig.get(piece.id).meta["warnings"] == []


# ---- a piece whose folder is gone --------------------------------------------------


def _gone(piece: S.Piece) -> str:
    """Remove the piece's folder (deleted by hand, or the data folder moved)."""
    shutil.rmtree(piece.workspace)
    return f"the piece's folder {piece.workspace} is missing"


def test_a_stage_whose_folder_is_gone_fails_the_piece_and_raises_nothing(rig):
    """The transcript cannot be opened: the piece fails and says why, its run is closed,
    and nothing is raised, so the run goes on to the editor's other requests."""
    piece = rig.new_piece(checkpoint=True)
    SS.research(rig.ctx, piece)
    missing = _gone(piece)

    out = SS.write(rig.ctx, rig.get(piece.id), "Lead with the durability.")

    p = rig.get(piece.id)
    assert out.stage == p.stage == S.STAGE_FAILED
    assert p.error == out.message == missing + SS.PUT_BACK
    assert p.meta["failed_stage"] == "write"
    last = S.list_runs(rig.conn, piece.id)[-1]
    assert (last.stage, last.outcome, last.detail) == ("write", "error", missing + SS.PUT_BACK)
    assert last.finished_at is not None


def test_a_transcript_that_cannot_be_written_fails_the_piece_with_the_reason(rig):
    piece = rig.new_piece(checkpoint=True)
    SS.research(rig.ctx, piece)
    rig.cli.fail("write", OSError(28, "No space left on device"))

    out = SS.write(rig.ctx, rig.get(piece.id))

    assert out.stage == S.STAGE_FAILED
    assert out.message == (
        "could not write the session's files: [Errno 28] No space left on device"
    )


def test_resuming_a_piece_whose_folder_is_gone_runs_no_session(rig):
    rig.cli.fail("write", stopped("killed"))
    piece = rig.new_piece()
    SS.research(rig.ctx, piece)
    missing = _gone(piece)
    calls = len(rig.cli.calls)

    out = SS.resume_interrupted(rig.ctx, rig.get(piece.id))

    p = rig.get(piece.id)
    assert len(rig.cli.calls) == calls
    assert (out.stage, p.stage, p.error) == (
        S.STAGE_FAILED,
        S.STAGE_FAILED,
        missing + SS.PUT_BACK,
    )
    assert p.meta["failed_stage"] == "write"


def test_revising_a_piece_whose_folder_is_gone_leaves_it_in_the_queue(rig):
    piece = rig.ready_piece()
    missing = _gone(piece)
    calls = len(rig.cli.calls)

    out = SS.revise(rig.ctx, rig.get(piece.id), "Shorter.")

    p = rig.get(piece.id)
    assert len(rig.cli.calls) == calls
    assert (out.stage, p.stage, p.error) == (S.STAGE_READY, S.STAGE_READY, missing)
    assert out.message == f"revision not started: {missing}"
    assert rig.draft(p).status == queue_store.STATUS_PENDING


# ---- Resume on a polish the queue would refuse ---------------------------------------


def test_resuming_a_polish_whose_draft_was_approved_meanwhile_spends_no_round(rig):
    """A revision's polish round was stopped, so the old draft stayed pending; the editor
    approved it, then pressed Resume. The queue would refuse the polished piece, so no round
    runs (each one could take an hour) and the piece says why."""
    rig.ctx.revisable = functools.partial(ingest.revise_blocker, rig.conn)
    piece = rig.ready_piece()
    rig.cli.fail("polish", stopped("killed"))
    rig.cli.work["revise"] = _rewrite_to_one_post
    SS.revise(rig.ctx, rig.get(piece.id), "Shorter.")
    stuck = rig.get(piece.id)
    assert (stuck.stage, stuck.meta["failed_stage"]) == (S.STAGE_INTERRUPTED, "polish")
    queue_store.approve(rig.conn, stuck.draft_id)
    calls = len(rig.cli.calls)

    out = SS.resume_interrupted(rig.ctx, stuck)

    p = rig.get(piece.id)
    blocked = f"draft {p.draft_id} is approved, not pending; reopen it in the queue before revising"
    assert len(rig.cli.calls) == calls
    assert (out.stage, p.stage, p.error) == (S.STAGE_INTERRUPTED, S.STAGE_INTERRUPTED, blocked)
    assert out.message == f"not resumed: {blocked}"
    assert p.meta["failed_stage"] == "polish"
    # reopened, the same Resume polishes and puts the revision in the queue
    queue_store.reopen(rig.conn, p.draft_id)
    assert SS.resume_interrupted(rig.ctx, rig.get(piece.id)).stage == S.STAGE_READY
    assert rig.cli.stages()[calls:] == ["polish"]
    assert rig.draft(rig.get(piece.id)).draft.thread == [POST_SINGLE]


# ---- a session the CLI no longer has -----------------------------------------------


def lost(session_id: str, *, where: str = "stderr") -> claude_cli.SessionResult:
    """How a --resume of a session the CLI has cleaned up comes back: the CLI says so (on
    stderr with no result line, or in an error result) and exits 1."""
    said = f"No conversation found with session ID: {session_id}"
    return claude_cli.SessionResult(
        session_id=session_id,
        ok=False,
        subtype="no_result" if where == "stderr" else "error_during_execution",
        text="" if where == "stderr" else said,
        terminal_reason="CLI exited 1",
        returncode=1,
        stderr_tail=f"{said}\n" if where == "stderr" else "",
    )


@pytest.mark.parametrize("where", ["stderr", "result"])
def test_a_lost_session_is_taken_over_by_a_fresh_one_that_reads_the_files(rig, where):
    """Write was stopped; weeks later Resume finds the CLI has cleaned the session up. A
    --resume could never work again, so a new session takes the piece over: the standing
    instructions again, a prompt that has it read the piece's files first, and a new id
    the piece keeps. The log and the runs say so."""
    rig.cli.fail("write", stopped("killed"))
    piece = rig.new_piece()
    SS.research(rig.ctx, piece)
    old = piece.session_id
    rig.cli.fail("write", lost(old, where=where))

    out = SS.resume_interrupted(rig.ctx, rig.get(piece.id))

    assert out.stage == S.STAGE_READY
    p = rig.get(piece.id)
    assert p.session_id != old and uuid.UUID(p.session_id)
    assert p.meta["lost_sessions"] == [old]
    refused, fresh = rig.cli.of("write")[-2:]
    assert (refused.kw["session_id"], refused.kw["resume"]) == (old, True)
    assert (fresh.kw["session_id"], fresh.kw["resume"], fresh.kw["system"]) == (
        p.session_id,
        False,
        SYSTEM,
    )
    assert fresh.prompt.startswith(FRESH_HEAD) and fresh.resumed
    for name in (P.FACTBASE_FILE, P.PIECE_FILE, P.FACTCHECK_FILE):
        assert name in fresh.prompt.split(AGAIN)[0]
    # the later stages resume the new session, not the lost one
    polish = rig.cli.of("polish")[-1]
    assert (polish.kw["session_id"], polish.kw["resume"]) == (p.session_id, True)
    runs = [(r.stage, r.outcome) for r in S.list_runs(rig.conn, piece.id)]
    assert runs[-3:] == [("write", "session_lost"), ("write", "ok"), ("polish", "ok")]
    note = f"the CLI no longer has session {old}"
    assert note in rig.log(p) and any(note in line for line in rig.echoed)


def test_a_revision_of_a_lost_session_is_written_by_a_fresh_one(rig):
    piece = rig.ready_piece()
    rig.cli.fail("revise", lost(piece.session_id))
    rig.cli.work["revise"] = _rewrite_to_one_post

    out = SS.revise(rig.ctx, rig.get(piece.id), "Shorter.")

    assert out.stage == S.STAGE_READY
    fresh = rig.cli.of("revise")[-1]
    assert fresh.prompt.startswith(FRESH_HEAD) and fresh.kw["resume"] is False
    assert "Shorter." in fresh.prompt
    assert rig.draft(rig.get(piece.id)).draft.thread == [POST_SINGLE]


def test_a_stage_stopped_for_another_reason_resumes_the_same_session(rig):
    """Only the CLI's own "no conversation found" starts a new session: a stage that was
    killed, ran out of turns or failed otherwise keeps its session for Resume."""
    piece = rig.new_piece(checkpoint=True)
    SS.research(rig.ctx, piece)
    for result in (stopped("killed"), stopped("no_result"), stopped("")):
        rig.cli.fail("write", result)
        SS.write(rig.ctx, rig.get(piece.id))
        assert rig.get(piece.id).session_id == piece.session_id
    assert not any(c.prompt.startswith(FRESH_HEAD) for c in rig.cli.calls)


# ---- a stage whose sub-agent the CLI stopped -------------------------------------------


def test_a_write_whose_fact_checker_was_killed_stops_and_resumes_to_finish_it(rig):
    """The CLI stopped the cold fact-check when the turn ended and reported success: the
    stage did not finish. The piece stops (interrupted, resumable) and says why in its log
    and its runs; Resume sends the write stage again, naming the reason."""
    killed = stopped(claude_cli.SUBAGENT_KILLED)
    rig.cli.fail("write", killed)
    piece = rig.new_piece()
    out = SS.research(rig.ctx, piece)

    p = rig.get(piece.id)
    assert out.stage == p.stage == S.STAGE_INTERRUPTED
    assert rig.cli.stages() == ["research", "write"]  # never polished, never queued
    assert rig.no_draft(p)
    assert p.error == killed.detail
    assert p.error.startswith("the CLI stopped 1 sub-agent(s) before they finished: ")
    log = rig.log(p)
    assert f"stage ended: {claude_cli.SUBAGENT_KILLED} after 5 turns" in log
    assert f"!!! write: {killed.detail}" in log
    runs = S.list_runs(rig.conn, piece.id)
    assert (runs[-1].stage, runs[-1].outcome, runs[-1].detail) == (
        "write",
        claude_cli.SUBAGENT_KILLED,
        killed.detail,
    )

    out = SS.resume_interrupted(rig.ctx, p)

    assert out.stage == S.STAGE_READY
    again = rig.cli.of("write")[-1]
    assert again.resumed and again.prompt.startswith(f"{RESUME_HEAD} ({killed.detail}).")


def test_a_piece_whose_cold_fact_check_never_reported_never_reaches_the_queue(rig):
    """The live run's other half (fixed with background tasks off in every CLI child): the
    write stage ended without factcheck.md. Each polish round asks for it; a piece that
    still has none after the last round fails rather than going into the queue."""

    def write_without_factcheck(ws: Path, call: Call) -> None:
        write_piece(ws)
        (ws / P.FACTCHECK_FILE).unlink()

    rig.cli.work["write"] = write_without_factcheck
    piece = rig.new_piece()
    out = SS.research(rig.ctx, piece)

    p = rig.get(piece.id)
    assert out.stage == p.stage == S.STAGE_FAILED
    assert p.error.startswith(f"still blocked after polishing: {qa.NO_FACTCHECK}")
    assert rig.no_draft(p)
    polish = rig.cli.of("polish")
    assert len(polish) == rig.ctx.cfg["max_polish_rounds"]
    assert all(f"- {qa.NO_FACTCHECK}" in c.prompt for c in polish)
