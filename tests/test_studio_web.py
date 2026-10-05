"""Step 10's pages (studio/web.py), as the control panel serves them (panel/app.py
includes the router, renders it in the shared layout and wires its buttons to the runs).

Every test goes through panel.app.app with a temp DB (DB_PATH, so the data folder is
tmp_path). The panel's JobManager.start is replaced in every test (`started`): a button
press records the steps it asked for and never launches `run_studio.py`.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient

from approval_queue import store as queue_store
from draft.schema import Draft
from panel import app as panel_app
from panel.jobs import Job, JobError
from studio import angles as A
from studio import prompt as P
from studio import store as S
from studio import web as studio_web
from studio.settings import DEFAULT_PLAYBOOK, PLAYBOOK_NAME, load_studio_config, playbook_path
from tests.conftest import seed_item

TOPIC = "next-gen CTLA-4"
NOTE = "Lead with the durability, drop the history."
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
EVERY_STAGE = S.STAGES


# ---- fixtures ------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def started(monkeypatch) -> list[list[str]]:
    """The panel's JobManager.start, recorded: the steps each button asked for, in order.
    Autouse, so no test can start a real studio run."""
    calls: list[list[str]] = []

    def start(steps: list[str], **kw: Any) -> Job:
        calls.append(list(steps))
        return Job(id=f"run{len(calls)}", steps=list(steps), started_at=panel_app._now())

    monkeypatch.setattr(panel_app.JOBS, "start", start)
    return calls


@pytest.fixture
def client(db_file, monkeypatch):
    # The panel wires the studio's buttons to its runs when it is imported; keep that
    # wiring for this test whatever an earlier test did to it.
    monkeypatch.setitem(studio_web._starter, "start", panel_app._start_studio)
    return TestClient(panel_app.app, follow_redirects=False)


@pytest.fixture
def sconn(conn):
    S.ensure_tables(conn)
    return conn


@pytest.fixture
def cfg(monkeypatch) -> dict[str, Any]:
    """The settings the studio page shows, as the test sets them."""
    c = load_studio_config()
    c["model"], c["effort"] = "writer-model", "max"
    c["auto"] = {"enabled": True, "max_new_per_day": 1, "min_hours_between": 6, "checkpoint": False}
    c["manual"] = {"checkpoint": True}
    monkeypatch.setattr(studio_web, "load_studio_config", lambda *a, **k: c)
    return c


def make_piece(
    conn,
    *,
    stage: str = S.STAGE_RESEARCHING,
    topic: str = TOPIC,
    workspace: Path | str = "/nowhere/studio_pieces/piece",
    meta: dict[str, Any] | None = None,
    **fields: Any,
) -> S.Piece:
    pid = S.create_piece(
        conn,
        origin=S.ORIGIN_MANUAL,
        topic=topic,
        cluster_id=None,
        requested_angle="",
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


def studio_draft(conn, piece: S.Piece) -> int:
    draft_id = queue_store.insert_draft(
        conn,
        item_id=queue_store.studio_item_id(piece.id),
        model="writer-model (studio)",
        draft=Draft(thread=["A finished long post."], suggested_visual="", why_it_matters="w"),
    )
    S.update_piece(conn, piece.id, draft_id=draft_id)
    return draft_id


def flash_of(response) -> str:
    assert response.status_code == 303, response.text
    query = urlsplit(response.headers["location"]).query
    return parse_qs(query).get("flash", [""])[0]


def path_of(response) -> str:
    assert response.status_code == 303, response.text
    return urlsplit(response.headers["location"]).path


def started_message(n: int, step: str) -> str:
    return f"started run run{n} ({step}); its log is on the runs page"


# ---- GET /studio ---------------------------------------------------------------------


def test_the_studio_page_before_any_piece(client, sconn, cfg):
    r = client.get("/studio")
    assert r.status_code == 200
    body = r.text
    assert "<h1>Studio</h1>" in body and "No pieces yet." in body
    assert "Queued topics" not in body
    assert "Each piece is one writer-model session at max effort" in body
    assert 'href="/studio/playbook"' in body
    # the run bar: its two steps, back to this page
    bar = body.split('class="runbar"')[1].split("</div>")[0]
    assert 'name="step" value="studio_now"' in bar and ">Start a piece now<" in bar
    assert 'name="step" value="studio_resume"' in bar and ">Act on requests<" in bar
    assert 'name="back" value="/studio"' in bar and "disabled" not in bar
    # the form: a topic, a story, every angle, the checkpoint ticked as configured
    form = body.split('action="/studio/topics"')[1].split("</form>")[0]
    assert 'name="topic"' in form and 'name="story"' in form
    for key, angle in A.load_angles().items():
        assert f'<option value="{key}">{angle.name}</option>' in form
    assert 'name="checkpoint" value="1" checked' in form
    assert (
        "Automatic pieces: on, at most 1 in any 24 hours and 6h apart, not stopping after "
        "research" in body
    )


def test_the_studio_page_follows_the_settings(client, sconn, cfg):
    cfg["manual"]["checkpoint"] = False
    cfg["auto"]["enabled"] = False
    body = client.get("/studio").text
    assert 'name="checkpoint" value="1" >' in body  # not ticked
    assert "Automatic pieces: off (studio/config.yaml)." in body


def test_the_studio_page_lists_pieces_newest_first_and_the_queued_topics(client, sconn, cfg):
    ready = make_piece(
        sconn,
        stage=S.STAGE_READY,
        title="The class is back",
        angle="class_deep_dive",
        shape="long_post",
    )
    draft_id = studio_draft(sconn, ready)
    failed = make_piece(sconn, stage=S.STAGE_FAILED, error="research: timed out " + "x" * 300)
    waiting = make_piece(sconn, stage=S.STAGE_RESEARCH_READY, topic="KRAS G12D deals")
    S.request(sconn, waiting.id, S.REQUEST_CONTINUE, NOTE)
    writing = make_piece(sconn, stage=S.STAGE_WRITING, topic="")
    discarded = make_piece(sconn, stage=S.STAGE_DISCARDED, topic="An old idea")
    words = S.queue_topic(sconn, topic="Merck KRAS G12D deal", cluster_id=12, angle="deal_decoder")
    story = S.queue_topic(sconn, cluster_id=34)
    claimed = S.queue_topic(sconn, topic="Claimed-topic-xyz")
    S.claim_topic(sconn, claimed, ready.id)

    body = client.get("/studio").text

    rows = body.split("<h2>Pieces</h2>")[1]
    order = [
        rows.index(f'<a href="/studio/{p.id}">{p.id}</a>')
        for p in (discarded, writing, waiting, failed, ready)
    ]
    assert order == sorted(order)  # newest first
    assert '<span class="pill ok">ready</span>' in rows
    assert "The class is back" in rows and "class_deep_dive" in rows and "long_post" in rows
    assert f'<a href="/drafts/{draft_id}">{draft_id}</a>' in rows
    assert '<span class="pill fail">failed</span>' in rows
    # the error, cut to its first 160 characters
    assert "research: timed out " + "x" * 140 in rows and "x" * 141 not in rows
    assert '<span class="pill warn">read the research</span>' in rows
    assert "continue requested" in rows
    assert '<span class="pill warn">writing</span>' in rows and f"piece {writing.id}" in rows
    assert '<span class="pill skip">discarded</span>' in rows

    topics = body.split("<h2>Queued topics</h2>")[1].split("</table>")[0]
    assert "Merck KRAS G12D deal" in topics and "(story 12)" in topics
    assert "deal_decoder" in topics and "story 34" in topics
    assert f'action="/studio/topics/{words}/drop"' in topics
    assert f'action="/studio/topics/{story}/drop"' in topics
    assert "Claimed-topic-xyz" not in body  # a started topic has left the queue


def test_the_studio_buttons_are_configured_steps_and_come_back_here(client, started):
    assert {"studio_now", "studio_resume"} <= set(panel_app.JOBS.step_names())
    r = client.post("/runs", data={"step": ["studio_now"], "back": "/studio"})
    assert r.status_code == 303 and r.headers["location"] == "/studio"
    assert started == [["studio_now"]]


def test_the_studio_buttons_wait_while_a_studio_run_holds_the_lock(client, sconn, monkeypatch):
    busy = {"studio", "studio_now", "studio_resume"}
    monkeypatch.setitem(studio_web.templates.env.globals, "busy_steps", lambda: busy)
    bar = client.get("/studio").text.split('class="runbar"')[1].split("</div>")[0]
    assert bar.count("disabled") == 2
    assert "studio_now is already running" in bar and "studio_resume is already running" in bar


# ---- POST /studio/topics -------------------------------------------------------------


def test_a_topic_is_queued_and_the_studio_started(client, sconn, started):
    r = client.post(
        "/studio/topics",
        data={"topic": f"  {TOPIC} ", "angle": "class_deep_dive", "checkpoint": "1"},
    )
    assert path_of(r) == "/studio"
    assert flash_of(r) == started_message(1, "studio_now")
    assert started == [["studio_now"]]
    [topic] = S.queued_topics(sconn)
    assert (topic.topic, topic.cluster_id, topic.angle, topic.checkpoint, topic.piece_id) == (
        TOPIC,
        None,
        "class_deep_dive",
        True,
        None,
    )
    # the flash is shown on the page the browser lands on
    assert started_message(1, "studio_now") in client.get(r.headers["location"]).text


def test_a_feed_story_is_queued_without_words_or_the_checkpoint(client, sconn, started):
    r = client.post("/studio/topics", data={"story": " 42 ", "angle": ""})
    assert flash_of(r) == started_message(1, "studio_now")
    [topic] = S.queued_topics(sconn)
    assert (topic.topic, topic.cluster_id, topic.angle, topic.checkpoint) == ("", 42, "", False)


def test_words_and_a_story_go_together(client, sconn):
    client.post("/studio/topics", data={"topic": "why now", "story": "7", "checkpoint": "1"})
    [topic] = S.queued_topics(sconn)
    assert (topic.topic, topic.cluster_id) == ("why now", 7)


@pytest.mark.parametrize(
    "data",
    [
        {},
        {"topic": "   ", "story": ""},
        {"angle": "deal_decoder", "checkpoint": "1"},
    ],
)
def test_nothing_to_write_about_is_refused(client, sconn, started, data):
    r = client.post("/studio/topics", data=data)
    assert path_of(r) == "/studio"
    assert flash_of(r) == "type a topic or pick a story first"
    assert S.queued_topics(sconn) == [] and started == []


def test_an_unknown_angle_is_refused(client, sconn, started):
    r = client.post("/studio/topics", data={"topic": TOPIC, "angle": "no_such_angle"})
    assert flash_of(r) == "unknown angle no_such_angle"
    assert S.queued_topics(sconn) == [] and started == []


@pytest.mark.parametrize("story", ["12a", "#12", "-3", "1.5", "²", "١٢"])
def test_a_story_id_that_is_not_a_feed_number_is_refused_not_dropped(client, sconn, started, story):
    """Not silently queued as a topic without its story, and never a server error
    ("²".isdigit() is true, int("²") raises)."""
    r = client.post("/studio/topics", data={"topic": TOPIC, "story": story})
    assert path_of(r) == "/studio"
    assert flash_of(r) == f"a story id is the number the feed shows, not {story!r}"
    assert S.queued_topics(sconn) == [] and started == []


def test_a_start_the_panel_refuses_leaves_the_topic_queued(client, sconn, monkeypatch):
    def refuse(steps: list[str], **kw: Any) -> Job:
        raise JobError("studio_now is already running")

    monkeypatch.setattr(panel_app.JOBS, "start", refuse)
    r = client.post("/studio/topics", data={"topic": TOPIC})
    assert (
        flash_of(r) == "saved; it starts with the next studio run (studio_now is already running)"
    )
    assert [t.topic for t in S.queued_topics(sconn)] == [TOPIC]


def test_without_a_panel_a_request_waits_for_the_next_studio_run(
    client, sconn, started, monkeypatch
):
    monkeypatch.setitem(studio_web._starter, "start", None)
    r = client.post("/studio/topics", data={"topic": TOPIC})
    assert flash_of(r) == "saved; the next automatic studio run picks it up"
    assert started == [] and len(S.queued_topics(sconn)) == 1


def test_a_queued_topic_can_be_dropped_until_it_starts(client, sconn):
    waiting = S.queue_topic(sconn, topic=TOPIC)
    claimed = S.queue_topic(sconn, topic="already started")
    piece = make_piece(sconn)
    S.claim_topic(sconn, claimed, piece.id)

    r = client.post(f"/studio/topics/{waiting}/drop")
    assert path_of(r) == "/studio" and flash_of(r) == "topic dropped"
    assert S.queued_topics(sconn) == []

    client.post(f"/studio/topics/{claimed}/drop")
    row = sconn.execute("SELECT piece_id FROM studio_topics WHERE id = ?", (claimed,)).fetchone()
    assert row is not None and row[0] == piece.id  # the started piece keeps its topic


# ---- the editor's buttons on a piece ---------------------------------------------------


def test_continue_records_the_note_and_starts_the_resume_step(client, sconn, started):
    piece = make_piece(sconn, stage=S.STAGE_RESEARCH_READY)
    r = client.post(f"/studio/{piece.id}/continue", data={"note": f"  {NOTE}  "})
    assert path_of(r) == f"/studio/{piece.id}"
    assert flash_of(r) == started_message(1, "studio_resume")
    after = get(sconn, piece.id)
    assert (after.request, after.request_note, after.stage) == (
        S.REQUEST_CONTINUE,
        NOTE,
        S.STAGE_RESEARCH_READY,
    )
    assert started == [["studio_resume"]]


def test_continue_without_a_note_is_fine(client, sconn):
    piece = make_piece(sconn, stage=S.STAGE_RESEARCH_READY)
    client.post(f"/studio/{piece.id}/continue")
    assert (get(sconn, piece.id).request, get(sconn, piece.id).request_note) == ("continue", "")


@pytest.mark.parametrize("stage", [s for s in EVERY_STAGE if s != S.STAGE_RESEARCH_READY])
def test_continue_is_only_for_a_piece_waiting_at_the_checkpoint(client, sconn, started, stage):
    piece = make_piece(sconn, stage=stage)
    r = client.post(f"/studio/{piece.id}/continue", data={"note": NOTE})
    assert path_of(r) == f"/studio/{piece.id}"
    assert flash_of(r) == f"the piece is {stage}, not waiting"
    assert get(sconn, piece.id).request == "" and started == []


def test_revise_records_the_note_and_starts_the_resume_step(client, sconn, started):
    piece = make_piece(sconn, stage=S.STAGE_READY)
    studio_draft(sconn, piece)
    r = client.post(f"/studio/{piece.id}/revise", data={"note": NOTE})
    assert path_of(r) == f"/studio/{piece.id}"
    assert flash_of(r) == started_message(1, "studio_resume")
    after = get(sconn, piece.id)
    assert (after.request, after.request_note) == (S.REQUEST_REVISE, NOTE)
    assert started == [["studio_resume"]]


def test_revise_needs_words(client, sconn, started):
    piece = make_piece(sconn, stage=S.STAGE_READY)
    studio_draft(sconn, piece)
    r = client.post(f"/studio/{piece.id}/revise", data={"note": "   "})
    assert flash_of(r) == "say what to change"
    assert get(sconn, piece.id).request == "" and started == []


@pytest.mark.parametrize("stage", [s for s in EVERY_STAGE if s != S.STAGE_READY])
def test_revise_is_only_for_a_finished_piece(client, sconn, started, stage):
    piece = make_piece(sconn, stage=stage)
    r = client.post(f"/studio/{piece.id}/revise", data={"note": NOTE})
    assert flash_of(r) == f"the piece is {stage}; only a finished piece is revised"
    assert get(sconn, piece.id).request == "" and started == []


NOT_PENDING = (
    "its draft is no longer pending in the queue (approved, rejected or posted); "
    "reopen it there first"
)


@pytest.mark.parametrize("decide", [queue_store.approve, queue_store.reject])
def test_revise_is_refused_once_the_draft_has_left_pending(client, sconn, started, decide):
    """The revised piece could not replace a decided draft; the queue's reopen comes first."""
    piece = make_piece(sconn, stage=S.STAGE_READY)
    decide(sconn, studio_draft(sconn, piece))
    r = client.post(f"/studio/{piece.id}/revise", data={"note": NOTE})
    assert flash_of(r) == NOT_PENDING
    assert get(sconn, piece.id).request == "" and started == []


def test_revise_is_refused_for_a_piece_whose_draft_is_gone(client, sconn, started):
    piece = make_piece(sconn, stage=S.STAGE_READY)
    assert flash_of(client.post(f"/studio/{piece.id}/revise", data={"note": NOTE})) == NOT_PENDING
    S.update_piece(sconn, piece.id, draft_id=9999)
    assert flash_of(client.post(f"/studio/{piece.id}/revise", data={"note": NOTE})) == NOT_PENDING
    assert started == []


def test_a_reopened_draft_can_be_revised_again(client, sconn, started):
    piece = make_piece(sconn, stage=S.STAGE_READY)
    draft_id = studio_draft(sconn, piece)
    queue_store.approve(sconn, draft_id)
    queue_store.reopen(sconn, draft_id)
    r = client.post(f"/studio/{piece.id}/revise", data={"note": NOTE})
    assert flash_of(r) == started_message(1, "studio_resume")
    assert get(sconn, piece.id).request == S.REQUEST_REVISE


@pytest.mark.parametrize("stage", [S.STAGE_FAILED, S.STAGE_INTERRUPTED])
def test_resume_asks_the_next_run_to_pick_the_piece_up(client, sconn, started, stage):
    piece = make_piece(sconn, stage=stage, error="write: timed out")
    r = client.post(f"/studio/{piece.id}/resume", data={"note": "keep it shorter"})
    assert path_of(r) == f"/studio/{piece.id}"
    assert flash_of(r) == started_message(1, "studio_resume")
    after = get(sconn, piece.id)
    assert (after.request, after.request_note, after.stage) == (
        S.REQUEST_CONTINUE,
        "keep it shorter",
        stage,
    )
    assert started == [["studio_resume"]]


@pytest.mark.parametrize(
    "stage", [s for s in EVERY_STAGE if s not in (S.STAGE_FAILED, S.STAGE_INTERRUPTED)]
)
def test_resume_is_only_for_a_failed_or_interrupted_piece(client, sconn, started, stage):
    piece = make_piece(sconn, stage=stage)
    r = client.post(f"/studio/{piece.id}/resume")
    assert flash_of(r) == f"the piece is {stage}; nothing to resume"
    assert get(sconn, piece.id).request == "" and started == []


@pytest.mark.parametrize("stage", S.RUNNING_STAGES)
def test_a_running_piece_cannot_be_discarded(client, sconn, stage):
    piece = make_piece(sconn, stage=stage)
    r = client.post(f"/studio/{piece.id}/discard")
    assert path_of(r) == f"/studio/{piece.id}"
    assert flash_of(r) == "stop the run on the runs page first"
    assert get(sconn, piece.id).stage == stage


@pytest.mark.parametrize(
    "stage",
    [
        S.STAGE_RESEARCH_READY,
        S.STAGE_READY,
        S.STAGE_FAILED,
        S.STAGE_INTERRUPTED,
        S.STAGE_DISCARDED,
    ],
)
def test_discarding_a_piece_drops_its_request_and_keeps_its_files(client, sconn, tmp_path, stage):
    ws = tmp_path / "studio_pieces" / "piece"
    (ws / P.POSTS_DIR).mkdir(parents=True)
    (ws / P.FACTBASE_FILE).write_text("# Fact base\n", encoding="utf-8")
    piece = make_piece(sconn, stage=stage, workspace=ws)
    S.update_piece(sconn, piece.id, request=S.REQUEST_CONTINUE, request_note=NOTE)
    r = client.post(f"/studio/{piece.id}/discard")
    assert path_of(r) == "/studio"
    assert flash_of(r) == f"piece {piece.id} discarded (its files stay in {ws})"
    after = get(sconn, piece.id)
    assert (after.stage, after.request, after.request_note) == (S.STAGE_DISCARDED, "", "")
    assert (ws / P.FACTBASE_FILE).is_file()


@pytest.mark.parametrize(
    "method, path",
    [
        ("get", "/studio/999"),
        ("get", "/studio/999/card/1"),
        ("post", "/studio/999/continue"),
        ("post", "/studio/999/revise"),
        ("post", "/studio/999/resume"),
        ("post", "/studio/999/discard"),
    ],
)
def test_an_unknown_piece_is_404(client, sconn, started, method, path):
    r = getattr(client, method)(path)
    assert r.status_code == 404
    assert started == []


# ---- GET /studio/{id}: one piece ---------------------------------------------------------


def write_workspace(ws: Path) -> None:
    """What a finished session leaves in its folder."""
    (ws / P.POSTS_DIR).mkdir(parents=True)
    (ws / P.CARDS_DIR).mkdir()
    (ws / P.POSTS_DIR / "01.txt").write_text("\ufeffThe first post: 41% ORR.\n", "utf-8")
    (ws / P.POSTS_DIR / "02.txt").write_text("The second post, its own section.", "utf-8")
    (ws / P.POSTS_DIR / "03.txt").write_text("   \n", encoding="utf-8")  # blank: not shown
    piece_json = {"posts": ["posts/02.txt", "posts/01.txt", "posts/03.txt", "posts/missing.txt"]}
    (ws / P.PIECE_FILE).write_text(json.dumps(piece_json), encoding="utf-8")
    for n in (10, 2, 1):
        (ws / P.CARDS_DIR / f"card_{n}.png").write_bytes(PNG)
    (ws / P.CARDS_DIR / "card_1.html").write_text("<html></html>", encoding="utf-8")
    (ws / P.CARDS_DIR / "card_x.png").write_bytes(PNG)
    (ws / P.FACTBASE_FILE).write_text("# Fact base\n\nFACTBASE-MARKER\n", encoding="utf-8")
    (ws / P.FACTCHECK_FILE).write_text("| post | claim |\nFACTCHECK-MARKER\n", "utf-8")
    (ws / "session.log").write_text(
        "=== research ===\n[WebSearch] KRAS G12D deals\n=== ready: draft 3 ===\n", "utf-8"
    )


def test_a_piece_page_shows_its_posts_cards_research_and_logs(client, sconn, tmp_path):
    ws = tmp_path / "studio_pieces" / "piece"
    write_workspace(ws)
    piece = make_piece(
        sconn,
        stage=S.STAGE_READY,
        workspace=ws,
        title="The class is back",
        meta={
            "research": {
                "summary": "A phase 2 readout reopens a class.",
                "why_now": "The readout landed this week.",
                "candidate_angles": [
                    {"angle": "class_deep_dive", "why": "the class is back"},
                    {"angle": "readout_reaction"},
                ],
            },
            "warnings": ["no closing disclaimer"],
            "problems": ["card card_2.html: text clipped"],
        },
    )
    draft_id = studio_draft(sconn, piece)
    run_id = S.start_run(sconn, piece.id, "research")
    S.finish_run(sconn, run_id, outcome="ok", detail="finished", turns=31, duration_ms=1_800_000)
    run_id = S.start_run(sconn, piece.id, "polish")
    S.finish_run(sconn, run_id, outcome="error_max_turns", detail="max_turns: ran out")

    r = client.get(f"/studio/{piece.id}")
    assert r.status_code == 200
    body = r.text

    assert "<h1>The class is back</h1>" in body
    assert f'<a href="/drafts/{draft_id}">draft {draft_id}</a>' in body
    # the posts in piece.json's order, without the blank or missing ones
    assert "The post (2 posts)" in body
    second = body.index("The second post, its own section.")
    first = body.index("The first post: 41% ORR.")
    assert second < first and "\ufeff" not in body
    # card pictures, by number, and only card_N.png
    cards = body.split("<h2>Cards</h2>")[1].split("</div>")[0]
    srcs = [f'src="/studio/{piece.id}/card/{n}"' for n in (1, 2, 10)]
    assert all(s in cards for s in srcs)
    assert [cards.index(s) for s in srcs] == sorted(cards.index(s) for s in srcs)
    assert "card_x" not in cards and "/card/x" not in cards
    # research, the fact base, the fact-check log, the session log and the runs
    assert "A phase 2 readout reopens a class." in body
    assert "Why now: The readout landed this week." in body
    assert "class_deep_dive (the class is back); readout_reaction" in body
    assert "FACTBASE-MARKER" in body and "FACTCHECK-MARKER" in body
    assert "[WebSearch] KRAS G12D deals" in body
    assert "no closing disclaimer" in body and "card card_2.html: text clipped" in body
    assert "error_max_turns" in body and "max_turns: ran out" in body
    assert "<td>31</td><td>30.0</td>" in body  # turns, minutes
    # a finished piece can be revised or discarded, not continued or resumed
    assert f'action="/studio/{piece.id}/revise"' in body
    assert f'action="/studio/{piece.id}/discard"' in body
    assert f'action="/studio/{piece.id}/continue"' not in body
    assert f'action="/studio/{piece.id}/resume"' not in body
    assert 'http-equiv="refresh"' not in body


def test_the_page_offers_what_the_stage_allows(client, sconn, tmp_path):
    ws = tmp_path / "studio_pieces" / "piece"
    write_workspace(ws)
    waiting = make_piece(sconn, stage=S.STAGE_RESEARCH_READY, workspace=ws)
    body = client.get(f"/studio/{waiting.id}").text
    assert f'action="/studio/{waiting.id}/continue"' in body
    assert "<details open><summary><strong>Fact base</strong>" in body  # read it first

    failed = make_piece(sconn, stage=S.STAGE_FAILED, workspace=ws, error="write: timed out")
    body = client.get(f"/studio/{failed.id}").text
    assert f'action="/studio/{failed.id}/resume"' in body
    assert '<p class="banner fail">write: timed out</p>' in body

    writing = make_piece(sconn, stage=S.STAGE_WRITING, workspace=ws)
    body = client.get(f"/studio/{writing.id}").text
    assert '<meta http-equiv="refresh" content="20">' in body
    assert f"/studio/{writing.id}/discard" not in body  # stop the run first
    for action in ("continue", "revise", "resume"):
        assert f'action="/studio/{writing.id}/{action}"' not in body

    gone = make_piece(sconn, stage=S.STAGE_DISCARDED, workspace=ws)
    assert f"/studio/{gone.id}/discard" not in client.get(f"/studio/{gone.id}").text


@pytest.mark.parametrize(
    "research, shown",
    [
        ({"summary": "S.", "candidate_angles": 3}, ""),
        ({"summary": "S.", "candidate_angles": True}, ""),
        ({"summary": "S.", "candidate_angles": "class_deep_dive"}, ""),
        ({"summary": "S.", "candidate_angles": {"angle": "the_race"}}, ""),
        (
            {"summary": "S.", "candidate_angles": [3, None, "x", {"angle": "the_race"}]},
            "Candidate angles:\n  the_race</p>",
        ),
        ("research.json was a string", ""),
        ([{"angle": "the_race"}], ""),
    ],
)
def test_a_research_json_of_the_wrong_shape_never_takes_the_page_down(
    client, sconn, tmp_path, research, shown
):
    """research.json is the session's writing, kept as the piece's research: the page
    shows the candidate angles that are entries of a list, and stays up whatever else it
    finds there (a number used to raise inside the template)."""
    piece = make_piece(
        sconn, stage=S.STAGE_RESEARCH_READY, workspace=tmp_path, meta={"research": research}
    )
    r = client.get(f"/studio/{piece.id}")
    assert r.status_code == 200
    assert f'action="/studio/{piece.id}/continue"' in r.text  # the editor can still act
    if shown:
        assert shown in r.text
    else:
        assert "Candidate angles" not in r.text


def test_a_piece_whose_folder_is_gone_still_has_a_page(client, sconn, tmp_path):
    piece = make_piece(sconn, stage=S.STAGE_INTERRUPTED, workspace=tmp_path / "gone")
    r = client.get(f"/studio/{piece.id}")
    assert r.status_code == 200
    assert "(nothing yet)" in r.text and "<h2>Cards</h2>" not in r.text


def test_the_session_writing_is_escaped_on_the_page(client, sconn, tmp_path):
    """The session writes its files from what it read on the web: never markup here."""
    ws = tmp_path / "studio_pieces" / "piece"
    (ws / P.POSTS_DIR).mkdir(parents=True)
    (ws / P.POSTS_DIR / "01.txt").write_text("<script>alert(1)</script> & more", "utf-8")
    (ws / P.FACTBASE_FILE).write_text("<img src=x onerror=alert(2)>", encoding="utf-8")
    piece = make_piece(
        sconn, stage=S.STAGE_READY, workspace=ws, meta={"research": {"summary": "<b>bold</b>"}}
    )
    body = client.get(f"/studio/{piece.id}").text
    assert "<script>alert(1)</script>" not in body and "<img src=x" not in body
    assert "&lt;script&gt;alert(1)&lt;/script&gt; &amp; more" in body
    assert "&lt;b&gt;bold&lt;/b&gt;" in body


@pytest.mark.parametrize(
    "piece_json",
    [
        None,  # no piece.json yet (the write stage is still running)
        "{ not json",
        json.dumps({"posts": "posts/01.txt"}),
        json.dumps(["posts/01.txt"]),
        json.dumps({"posts": ["../outside.txt", "/etc/hostname"]}),
    ],
)
def test_without_a_usable_post_list_the_posts_folder_is_shown_in_order(
    client, sconn, tmp_path, piece_json
):
    ws = tmp_path / "studio_pieces" / "piece"
    (ws / P.POSTS_DIR).mkdir(parents=True)
    (ws / P.POSTS_DIR / "02.txt").write_text("SECOND-POST", encoding="utf-8")
    (ws / P.POSTS_DIR / "01.txt").write_text("FIRST-POST", encoding="utf-8")
    (ws / P.POSTS_DIR / "notes.md").write_text("NOT-A-POST", encoding="utf-8")
    (tmp_path / "studio_pieces" / "outside.txt").write_text("OUTSIDE-SECRET", "utf-8")
    if piece_json is not None:
        (ws / P.PIECE_FILE).write_text(piece_json, encoding="utf-8")
    piece = make_piece(sconn, stage=S.STAGE_WRITING, workspace=ws)
    body = client.get(f"/studio/{piece.id}").text
    assert body.index("FIRST-POST") < body.index("SECOND-POST")
    assert "NOT-A-POST" not in body and "OUTSIDE-SECRET" not in body


def test_piece_json_cannot_show_a_file_outside_the_pieces_folder(client, sconn, tmp_path):
    """piece.json is the session's writing: a post path that leaves the folder (as studio/qa.py
    refuses it) is never read onto the page, whatever else the list holds."""
    ws = tmp_path / "studio_pieces" / "piece"
    (ws / P.POSTS_DIR).mkdir(parents=True)
    (ws / P.POSTS_DIR / "01.txt").write_text("THE-REAL-POST", encoding="utf-8")
    (tmp_path / ".env").write_text("X_API_SECRET=do-not-show", encoding="utf-8")
    posts = ["posts/01.txt", "../../.env", str(tmp_path / ".env"), "posts/../../../.env"]
    (ws / P.PIECE_FILE).write_text(json.dumps({"posts": posts}), encoding="utf-8")
    piece = make_piece(sconn, stage=S.STAGE_READY, workspace=ws)
    body = client.get(f"/studio/{piece.id}").text
    assert "THE-REAL-POST" in body
    assert "do-not-show" not in body


def test_the_session_log_shows_its_newest_lines(client, sconn, tmp_path):
    ws = tmp_path / "studio_pieces" / "piece"
    ws.mkdir(parents=True)
    lines = [f"log line {i:05d}" for i in range(1, 501)]
    (ws / "session.log").write_text("\n".join(lines) + "\n", encoding="utf-8")
    piece = make_piece(sconn, stage=S.STAGE_WRITING, workspace=ws)
    log = client.get(f"/studio/{piece.id}").text.split("<h2>Session log</h2>")[1]
    shown = [line for line in lines if line in log]
    assert shown == lines[-studio_web.LOG_TAIL_LINES :]


def test_a_long_session_log_still_shows_its_end(client, sconn, tmp_path):
    """A log of many megabytes (a long research stage and several revisions): the page
    shows its last lines, not the lines where some read limit happened to stop."""
    ws = tmp_path / "studio_pieces" / "piece"
    ws.mkdir(parents=True)
    with open(ws / "session.log", "w", encoding="utf-8") as fh:
        for i in range(45_000):
            fh.write(f"[WebFetch] https://example.org/a/very/long/source/page/{i:06d} " + "x" * 40)
            fh.write("\n")
        fh.write("=== ready: draft 3 ===\n")
    assert (ws / "session.log").stat().st_size > studio_web.LOG_TAIL_BYTES
    piece = make_piece(sconn, stage=S.STAGE_READY, workspace=ws)
    log = client.get(f"/studio/{piece.id}").text.split("<h2>Session log</h2>")[1]
    assert "=== ready: draft 3 ===" in log
    assert "page/044999" in log and "page/000000" not in log
    assert log.count("[WebFetch]") == studio_web.LOG_TAIL_LINES - 1


# ---- GET /studio/{id}/card/{n} ---------------------------------------------------------


def test_the_card_route_serves_the_pieces_card_pictures_only(client, sconn, tmp_path):
    ws = tmp_path / "studio_pieces" / "piece"
    write_workspace(ws)
    (ws / P.CARDS_DIR / "card_3.html").write_text("<html>no picture yet</html>", "utf-8")
    piece = make_piece(sconn, stage=S.STAGE_READY, workspace=ws)

    r = client.get(f"/studio/{piece.id}/card/2")
    assert r.status_code == 200
    assert r.headers["content-type"] == "image/png" and r.content == PNG

    assert client.get(f"/studio/{piece.id}/card/3").status_code == 404  # html, no png
    assert client.get(f"/studio/{piece.id}/card/4").status_code == 404
    assert client.get(f"/studio/{piece.id}/card/-1").status_code == 404
    assert client.get(f"/studio/{piece.id}/card/x").status_code == 422  # not a number
    for sneaky in ("..%2F..%2Ffactbase.md", "1.html", "1%2Fx"):
        r = client.get(f"/studio/{piece.id}/card/{sneaky}")
        assert r.status_code in (404, 422) and b"FACTBASE-MARKER" not in r.content


def test_one_pieces_card_route_never_serves_anothers(client, sconn, tmp_path):
    ws = tmp_path / "studio_pieces" / "piece"
    write_workspace(ws)
    with_cards = make_piece(sconn, stage=S.STAGE_READY, workspace=ws)
    other = make_piece(sconn, stage=S.STAGE_READY, workspace=tmp_path / "studio_pieces" / "other")
    assert client.get(f"/studio/{with_cards.id}/card/1").status_code == 200
    assert client.get(f"/studio/{other.id}/card/1").status_code == 404


# ---- the playbook --------------------------------------------------------------------


def test_the_playbook_is_the_seed_until_the_editor_saves_a_copy(client, tmp_path):
    seed = DEFAULT_PLAYBOOK.read_bytes()
    copy = tmp_path / PLAYBOOK_NAME

    body = client.get("/studio/playbook").text
    assert "This is the shipped seed; saving creates your own copy" in body
    assert DEFAULT_PLAYBOOK.read_text(encoding="utf-8").splitlines()[0] in body
    assert 'action="/studio/playbook/reset"' not in body

    r = client.post(
        "/studio/playbook", data={"text": "# Playbook\r\n\r\nLead with the number.\r\n\r\n\r\n"}
    )
    assert path_of(r) == "/studio/playbook"
    assert flash_of(r) == "saved; the next session reads it"
    assert copy.read_text(encoding="utf-8") == "# Playbook\n\nLead with the number.\n"
    assert not copy.with_suffix(".tmp").exists()
    assert playbook_path(tmp_path) == copy  # what the next session is handed
    assert DEFAULT_PLAYBOOK.read_bytes() == seed  # the shipped seed is never written

    body = client.get("/studio/playbook").text
    assert f"This is your edited copy ({copy})" in body
    assert "Lead with the number." in body
    assert 'action="/studio/playbook/reset"' in body

    r = client.post("/studio/playbook/reset")
    assert path_of(r) == "/studio/playbook"
    assert flash_of(r) == "back to the shipped seed (playbook.md)"
    assert not copy.exists() and playbook_path(tmp_path) == DEFAULT_PLAYBOOK
    assert "This is the shipped seed" in client.get("/studio/playbook").text
    # resetting again is harmless
    assert flash_of(client.post("/studio/playbook/reset")).startswith("back to the shipped seed")


@pytest.mark.parametrize("data", [{}, {"text": ""}, {"text": "  \r\n \n\t"}])
def test_an_empty_playbook_is_not_saved(client, tmp_path, data):
    (tmp_path / PLAYBOOK_NAME).write_text("THE EDITOR'S COPY\n", encoding="utf-8")
    r = client.post("/studio/playbook", data=data)
    assert flash_of(r) == "the playbook cannot be empty"
    assert (tmp_path / PLAYBOOK_NAME).read_text(encoding="utf-8") == "THE EDITOR'S COPY\n"


# ---- the studio in the rest of the panel -------------------------------------------------


def test_the_nav_links_the_studio_on_every_kind_of_page(client, sconn):
    for path in ("/", "/feed", "/queue", "/studio", "/studio/playbook"):
        body = client.get(path).text
        assert '<a href="/studio">Studio</a>' in body, path


def test_the_feed_offers_a_studio_piece_for_each_story(client, conn, started):
    cid = seed_item(conn, "i1", source="biorxiv", total=42)
    body = client.get("/feed?all=1").text
    form = body.split('action="/studio/topics"')[1].split("</form>")[0]
    assert f'<input type="hidden" name="story" value="{cid}">' in form
    assert '<input type="hidden" name="checkpoint" value="1">' in form
    assert ">Write a studio piece</button>" in form

    # pressing it queues the story, to stop after research, and starts the studio
    r = client.post("/studio/topics", data={"story": str(cid), "checkpoint": "1"})
    assert flash_of(r) == started_message(1, "studio_now")
    S.ensure_tables(conn)
    [topic] = S.queued_topics(conn)
    assert (topic.topic, topic.cluster_id, topic.checkpoint) == ("", cid, True)
    assert started == [["studio_now"]]
