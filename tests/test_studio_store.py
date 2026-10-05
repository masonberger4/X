"""Step 10's tables (studio/store.py): studio_pieces, studio_runs and studio_topics.

They live in the shared pipeline DB beside the queue's tables, opened the way the studio's
runner opens it (approval_queue/store.py:connect, then ensure_tables). Every timestamp the
store writes comes from `store._now`, which the tests replace with a settable clock.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import UTC, datetime, timedelta

import pytest

from studio import store as S

T0 = datetime(2026, 10, 5, 12, 0, tzinfo=UTC)


class Clock:
    """A settable stand-in for store._now (aware UTC, seconds, as the store writes them)."""

    def __init__(self, start: datetime = T0) -> None:
        self.now = start

    def __call__(self) -> str:
        return self.now.isoformat(timespec="seconds")

    def advance(self, **delta: float) -> str:
        self.now += timedelta(**delta)
        return self()


@pytest.fixture
def sconn(conn):
    """The shared pipeline DB (step 1's and the queue's tables) with the studio's tables."""
    S.ensure_tables(conn)
    return conn


@pytest.fixture
def clock(monkeypatch):
    c = Clock()
    monkeypatch.setattr(S, "_now", c)
    return c


def _piece(conn: sqlite3.Connection, **over) -> int:
    fields = dict(
        origin=S.ORIGIN_MANUAL,
        topic="next-gen CTLA-4",
        cluster_id=None,
        requested_angle="",
        checkpoint=True,
        session_id="0f0e0d0c-0000-4000-8000-000000000001",
        workspace="/data/studio_pieces/20261005-120000-next-gen-ctla-4",
        model="writer-model",
        effort="max",
    )
    fields.update(over)
    return S.create_piece(conn, **fields)


def _get(conn: sqlite3.Connection, piece_id: int) -> S.Piece:
    piece = S.get_piece(conn, piece_id)
    assert piece is not None
    return piece


# ---- the schema --------------------------------------------------------------------


def _tables(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
    return {r[0] for r in rows}


def test_connect_creates_the_three_tables_and_can_be_repeated(tmp_path):
    path = tmp_path / "pipeline.db"
    first = S.connect(path)
    pid = _piece(first)
    first.close()
    again = S.connect(path)  # ensure_tables on an existing file keeps what is there
    try:
        assert {"studio_pieces", "studio_runs", "studio_topics"} <= _tables(again)
        S.ensure_tables(again)
        assert again.row_factory is sqlite3.Row
        assert _get(again, pid).topic == "next-gen CTLA-4"
    finally:
        again.close()


def test_the_tables_sit_beside_the_queue_tables(sconn):
    tables = _tables(sconn)
    assert {"studio_pieces", "studio_runs", "studio_topics"} <= tables
    assert {"drafts", "decisions", "items", "clusters"} <= tables


def test_a_connection_may_be_used_from_another_thread(tmp_path):
    """The panel opens it in one worker thread and may use it in another."""
    conn = S.connect(tmp_path / "pipeline.db")
    errors: list[BaseException] = []

    def work() -> None:
        try:
            _piece(conn)
        except BaseException as exc:  # pragma: no cover - the failure being guarded
            errors.append(exc)

    worker = threading.Thread(target=work)
    worker.start()
    worker.join()
    try:
        assert errors == []
        assert len(S.list_pieces(conn)) == 1
    finally:
        conn.close()


# ---- pieces ------------------------------------------------------------------------


def test_a_new_piece_starts_researching_with_nothing_else_set(sconn, clock):
    pid = _piece(sconn, origin=S.ORIGIN_AUTO, topic="", cluster_id=42, checkpoint=False)
    p = _get(sconn, pid)
    assert p.id == pid
    assert p.stage == S.STAGE_RESEARCHING
    assert p.origin == S.ORIGIN_AUTO
    assert p.cluster_id == 42
    assert p.checkpoint is False
    assert p.created_at == p.updated_at == clock()
    assert (p.session_id, p.model, p.effort) == (
        "0f0e0d0c-0000-4000-8000-000000000001",
        "writer-model",
        "max",
    )
    assert p.workspace.endswith("next-gen-ctla-4")
    assert p.meta == {}
    assert p.draft_id is None
    assert (p.angle, p.shape, p.hook_style, p.title) == ("", "", "", "")
    assert (p.request, p.request_note, p.error) == ("", "", "")


def test_checkpoint_comes_back_as_a_bool(sconn):
    assert _get(sconn, _piece(sconn, checkpoint=True)).checkpoint is True
    assert _get(sconn, _piece(sconn, checkpoint=False)).checkpoint is False


def test_the_label_is_the_title_then_the_topic_then_the_number(sconn):
    pid = _piece(sconn, topic="")
    assert _get(sconn, pid).label == f"piece {pid}"
    S.update_piece(sconn, pid, topic="next-gen CTLA-4")
    assert _get(sconn, pid).label == "next-gen CTLA-4"
    S.update_piece(sconn, pid, title="CTLA-4 is back")
    assert _get(sconn, pid).label == "CTLA-4 is back"


def test_an_unknown_piece_is_none(sconn):
    assert S.get_piece(sconn, 999) is None


def test_list_pieces_is_newest_first_and_limited(sconn):
    ids = [_piece(sconn, topic=f"topic {n}") for n in range(4)]
    assert [p.id for p in S.list_pieces(sconn)] == ids[::-1]
    assert [p.id for p in S.list_pieces(sconn, limit=2)] == ids[:1:-1]


def test_update_piece_changes_only_the_named_fields(sconn, clock):
    pid = _piece(sconn)
    before = _get(sconn, pid)
    later = clock.advance(minutes=5)
    S.update_piece(sconn, pid, stage=S.STAGE_READY, draft_id=7, title="CTLA-4 is back")
    after = _get(sconn, pid)
    assert (after.stage, after.draft_id, after.title) == (S.STAGE_READY, 7, "CTLA-4 is back")
    assert after.updated_at == later
    assert after.created_at == before.created_at
    assert (after.topic, after.session_id, after.meta) == (before.topic, before.session_id, {})


def test_update_piece_can_clear_a_story(sconn):
    pid = _piece(sconn, cluster_id=5)
    S.update_piece(sconn, pid, cluster_id=None)
    assert _get(sconn, pid).cluster_id is None


@pytest.mark.parametrize(
    "name",
    ["origin", "workspace", "model", "effort", "checkpoint", "created_at", "meta_json", "id"],
)
def test_update_piece_refuses_a_field_it_does_not_own(sconn, name):
    pid = _piece(sconn)
    before = _get(sconn, pid)
    with pytest.raises(ValueError, match="cannot update"):
        S.update_piece(sconn, pid, title="changed", **{name: "x"})
    assert _get(sconn, pid) == before  # nothing at all was written


def test_update_piece_refuses_an_unknown_stage(sconn):
    pid = _piece(sconn)
    with pytest.raises(ValueError, match="unknown stage 'published'"):
        S.update_piece(sconn, pid, stage="published", error="never written")
    p = _get(sconn, pid)
    assert (p.stage, p.error) == (S.STAGE_RESEARCHING, "")


def test_every_known_stage_is_accepted(sconn):
    pid = _piece(sconn)
    for stage in S.STAGES:
        S.update_piece(sconn, pid, stage=stage)
        assert _get(sconn, pid).stage == stage


def test_meta_is_merged_key_by_key(sconn, clock):
    pid = _piece(sconn)
    S.update_piece(sconn, pid, meta={"research": {"topic": "A", "story_id": 4}})
    S.update_piece(sconn, pid, meta={"warnings": ["w1"]})
    assert _get(sconn, pid).meta == {"research": {"topic": "A", "story_id": 4}, "warnings": ["w1"]}
    # One level deep: a key that is given again is replaced whole, not merged into.
    S.update_piece(sconn, pid, meta={"research": {"topic": "B"}, "failed_stage": "write"})
    assert _get(sconn, pid).meta == {
        "research": {"topic": "B"},
        "warnings": ["w1"],
        "failed_stage": "write",
    }
    # A field update without meta leaves it alone, and so does an empty meta.
    S.update_piece(sconn, pid, error="boom")
    S.update_piece(sconn, pid, meta={})
    assert _get(sconn, pid).meta["failed_stage"] == "write"
    # Meta alone still counts as a change of the piece.
    later = clock.advance(seconds=30)
    S.update_piece(sconn, pid, meta={"problems": []})
    assert _get(sconn, pid).updated_at == later


def test_meta_keeps_non_ascii_text_as_written(sconn):
    pid = _piece(sconn)
    note = "Lead with the 41% response rate — ORR, not PFS · résumé"
    S.update_piece(sconn, pid, meta={"revise_note": note})
    raw = sconn.execute("SELECT meta_json FROM studio_pieces WHERE id = ?", (pid,)).fetchone()[0]
    assert note in raw  # stored readable (ensure_ascii=False), not \u-escaped
    assert _get(sconn, pid).meta["revise_note"] == note


@pytest.mark.parametrize("raw", ["not json", "[1, 2]", '"text"', ""])
def test_unreadable_meta_reads_as_empty_and_a_merge_replaces_it(sconn, raw):
    pid = _piece(sconn)
    sconn.execute("UPDATE studio_pieces SET meta_json = ? WHERE id = ?", (raw, pid))
    assert _get(sconn, pid).meta == {}
    S.update_piece(sconn, pid, meta={"failed_stage": "polish"})
    assert _get(sconn, pid).meta == {"failed_stage": "polish"}


# ---- requests ----------------------------------------------------------------------


def test_a_request_is_recorded_with_its_note_trimmed(sconn):
    pid = _piece(sconn)
    S.request(sconn, pid, S.REQUEST_CONTINUE, "  lead with the deal terms \n")
    p = _get(sconn, pid)
    assert (p.request, p.request_note) == (S.REQUEST_CONTINUE, "lead with the deal terms")
    assert [q.id for q in S.pending_requests(sconn)] == [pid]


def test_an_unknown_request_is_refused(sconn):
    pid = _piece(sconn)
    with pytest.raises(ValueError, match="unknown request 'publish'"):
        S.request(sconn, pid, "publish", "post it now")
    assert _get(sconn, pid).request == ""
    assert S.pending_requests(sconn) == []


def test_pending_requests_come_oldest_first_and_leave_when_cleared(sconn, clock):
    a, b, c = (_piece(sconn, topic=t) for t in ("a", "b", "c"))
    S.request(sconn, c, S.REQUEST_REVISE, "shorter")
    clock.advance(minutes=1)
    S.request(sconn, b, S.REQUEST_CONTINUE)
    S.request(sconn, a, S.REQUEST_CONTINUE)  # the same second as b: the lower id first
    assert [p.id for p in S.pending_requests(sconn)] == [c, a, b]
    S.update_piece(sconn, c, request="", request_note="")
    assert [p.id for p in S.pending_requests(sconn)] == [a, b]


def test_running_pieces_are_the_ones_mid_stage(sconn):
    by_stage = {}
    for stage in S.STAGES:
        pid = _piece(sconn, topic=stage)
        S.update_piece(sconn, pid, stage=stage)
        by_stage[stage] = pid
    running = S.running_pieces(sconn)
    assert [p.id for p in running] == sorted(by_stage[s] for s in S.RUNNING_STAGES)
    assert {p.stage for p in running} == set(S.RUNNING_STAGES)
    assert S.STAGE_RESEARCH_READY not in S.RUNNING_STAGES  # waiting for a human is not running


def test_recent_pieces_are_written_pieces_newest_first(sconn):
    unwritten = _piece(sconn, topic="no angle yet")
    written = []
    for angle in ("deal_decoder", "class_deep_dive", "catalyst_map"):
        pid = _piece(sconn, topic=angle)
        S.update_piece(sconn, pid, angle=angle, stage=S.STAGE_READY)
        written.append(pid)
    gone = _piece(sconn, topic="dropped")
    S.update_piece(sconn, gone, angle="deal_decoder", stage=S.STAGE_DISCARDED)
    failed = _piece(sconn, topic="failed after writing")
    S.update_piece(sconn, failed, angle="catalyst_map", stage=S.STAGE_FAILED)

    ids = [p.id for p in S.recent_pieces(sconn, 10)]
    assert ids == [failed, *written[::-1]]
    assert unwritten not in ids and gone not in ids
    assert [p.id for p in S.recent_pieces(sconn, 2)] == [failed, written[-1]]


def test_pieces_since_counts_by_start_time_and_origin(sconn, clock):
    first = _piece(sconn, origin=S.ORIGIN_AUTO)
    t1 = clock.advance(hours=1)
    _piece(sconn, origin=S.ORIGIN_MANUAL)
    clock.advance(hours=1)
    _piece(sconn, origin=S.ORIGIN_AUTO)
    # A later change to the first piece does not make it count as new.
    clock.advance(hours=1)
    S.update_piece(sconn, first, stage=S.STAGE_READY)

    assert S.pieces_since(sconn, T0.isoformat(timespec="seconds")) == 3
    assert S.pieces_since(sconn, t1) == 2  # the boundary counts
    assert S.pieces_since(sconn, t1, origin=S.ORIGIN_AUTO) == 1
    assert S.pieces_since(sconn, t1, origin=S.ORIGIN_MANUAL) == 1
    assert S.pieces_since(sconn, clock()) == 0


def test_last_started_is_the_newest_start_not_the_newest_change(sconn, clock):
    assert S.last_started(sconn) is None
    first = _piece(sconn)
    second_start = clock.advance(hours=2)
    _piece(sconn)
    clock.advance(hours=3)
    S.update_piece(sconn, first, title="touched later")
    assert S.last_started(sconn) == second_start


def test_used_cluster_ids_count_every_piece_and_queued_topic(sconn):
    failed = _piece(sconn, cluster_id=5)
    S.update_piece(sconn, failed, stage=S.STAGE_FAILED)
    gone = _piece(sconn, cluster_id=7)
    S.update_piece(sconn, gone, stage=S.STAGE_DISCARDED)
    _piece(sconn, cluster_id=None)
    S.queue_topic(sconn, cluster_id=9)
    claimed = S.queue_topic(sconn, cluster_id=11)
    S.claim_topic(sconn, claimed, failed)
    S.queue_topic(sconn, topic="words only")
    S.queue_topic(sconn, cluster_id=5)  # the same story twice is one id

    assert S.used_cluster_ids(sconn) == {5, 7, 9, 11}


def test_used_cluster_ids_is_empty_on_a_new_database(sconn):
    assert S.used_cluster_ids(sconn) == set()


# ---- runs --------------------------------------------------------------------------


def test_a_run_is_open_until_it_is_finished(sconn, clock):
    pid = _piece(sconn)
    started = clock()
    rid = S.start_run(sconn, pid, "research")
    [run] = S.list_runs(sconn, pid)
    assert run == S.RunRow(
        id=rid,
        piece_id=pid,
        stage="research",
        started_at=started,
        finished_at=None,
        outcome="",
        detail="",
        turns=0,
        cost_usd=0.0,
        duration_ms=0,
    )
    finished = clock.advance(minutes=90)
    S.finish_run(
        sconn, rid, outcome="ok", detail="finished", turns=57, cost_usd=12.5, duration_ms=5_400_000
    )
    [run] = S.list_runs(sconn, pid)
    assert (run.finished_at, run.outcome, run.detail) == (finished, "ok", "finished")
    assert (run.turns, run.cost_usd, run.duration_ms) == (57, 12.5, 5_400_000)
    assert isinstance(run.cost_usd, float)


def test_a_long_run_detail_is_cut_to_2000_characters(sconn):
    pid = _piece(sconn)
    rid = S.start_run(sconn, pid, "write")
    S.finish_run(sconn, rid, outcome="error_during_execution", detail="x" * 5000)
    [run] = S.list_runs(sconn, pid)
    assert run.detail == "x" * 2000


def test_runs_are_listed_per_piece_in_order(sconn):
    a, b = _piece(sconn), _piece(sconn)
    ra1 = S.start_run(sconn, a, "research")
    rb1 = S.start_run(sconn, b, "research")
    ra2 = S.start_run(sconn, a, "write")
    assert [(r.id, r.stage) for r in S.list_runs(sconn, a)] == [(ra1, "research"), (ra2, "write")]
    assert [r.id for r in S.list_runs(sconn, b)] == [rb1]
    assert S.list_runs(sconn, 999) == []


def test_close_open_runs_closes_only_that_pieces_unfinished_runs(sconn, clock):
    a, b = _piece(sconn), _piece(sconn)
    done = S.start_run(sconn, a, "research")
    S.finish_run(sconn, done, outcome="ok", detail="finished", turns=3)
    open_a = S.start_run(sconn, a, "write")
    open_b = S.start_run(sconn, b, "research")
    closed_at = clock.advance(minutes=10)

    S.close_open_runs(sconn, a, "the run stopped before the stage finished")

    runs = {r.id: r for r in S.list_runs(sconn, a) + S.list_runs(sconn, b)}
    assert (runs[done].outcome, runs[done].detail, runs[done].turns) == ("ok", "finished", 3)
    assert runs[open_a].outcome == "interrupted"
    assert runs[open_a].detail == "the run stopped before the stage finished"
    assert runs[open_a].finished_at == closed_at
    assert runs[open_b].finished_at is None and runs[open_b].outcome == ""


# ---- queued topics -----------------------------------------------------------------


@pytest.mark.parametrize("topic", ["", "   ", "\n\t"])
def test_a_queued_topic_needs_words_or_a_story(sconn, topic):
    with pytest.raises(ValueError, match="needs words or a story"):
        S.queue_topic(sconn, topic=topic)
    assert S.queued_topics(sconn) == []


def test_a_queued_topic_is_stored_trimmed_with_its_checkpoint(sconn, clock):
    tid = S.queue_topic(sconn, topic="  next-gen CTLA-4  ", angle=" class_deep_dive ")
    story = S.queue_topic(sconn, cluster_id=42, checkpoint=False)
    first, second = S.queued_topics(sconn)
    assert first == S.QueuedTopic(
        id=tid,
        created_at=clock(),
        topic="next-gen CTLA-4",
        cluster_id=None,
        angle="class_deep_dive",
        checkpoint=True,
        piece_id=None,
    )
    assert (second.id, second.topic, second.cluster_id) == (story, "", 42)
    assert second.checkpoint is False


def test_queued_topics_are_taken_in_order_and_claimed_once(sconn):
    t1 = S.queue_topic(sconn, topic="first")
    t2 = S.queue_topic(sconn, cluster_id=42)
    t3 = S.queue_topic(sconn, topic="third")
    assert S.next_queued_topic(sconn).id == t1

    piece = _piece(sconn)
    S.claim_topic(sconn, t1, piece)
    assert S.next_queued_topic(sconn).id == t2
    assert [t.id for t in S.queued_topics(sconn)] == [t2, t3]

    # A claimed topic belongs to its piece: dropping it does nothing.
    S.drop_topic(sconn, t1)
    row = sconn.execute("SELECT piece_id FROM studio_topics WHERE id = ?", (t1,)).fetchone()
    assert row is not None and row[0] == piece

    S.drop_topic(sconn, t3)
    assert sconn.execute("SELECT 1 FROM studio_topics WHERE id = ?", (t3,)).fetchone() is None
    S.drop_topic(sconn, 999)  # an unknown id is not an error

    S.claim_topic(sconn, t2, _piece(sconn))
    assert S.next_queued_topic(sconn) is None
    assert S.queued_topics(sconn) == []


def test_meta_round_trips_through_json(sconn):
    pid = _piece(sconn)
    meta = {"research": {"companies": [{"name": "Merck", "ticker": "MRK"}]}, "warnings": []}
    S.update_piece(sconn, pid, meta=meta)
    raw = sconn.execute("SELECT meta_json FROM studio_pieces WHERE id = ?", (pid,)).fetchone()[0]
    assert json.loads(raw) == meta == _get(sconn, pid).meta
