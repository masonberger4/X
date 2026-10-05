"""Step 10's learning loop against the database: what X says about the posted pieces
(studio/evidence.py), the brief that carries it (studio/runner.py:build_brief), the
`--learn` run and the playbook rewrite (studio/playbook.py), and the performance page
(studio/web.py, studio/dashboard.py).

The DB is a temp file (DB_PATH) holding every step's tables: posts come from step 3's
own `record_post`, snapshots from step 4's own `record_tweet_metrics`. The one model
call, the playbook rewrite, is faked (`rewriter`); tests/conftest.py refuses the real CLI.
"""

from __future__ import annotations

import json
import random
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient

import claude_cli
import run_studio
from approval_queue import store as queue_store
from draft.schema import Draft
from feedback import store as feedback_store
from feedback.models import Metrics, TweetMetrics
from panel import app as panel_app
from panel.jobs import Job
from publish import store as publish_store
from studio import angles as A
from studio import dashboard as D
from studio import evidence as E
from studio import learn as L
from studio import playbook as PB
from studio import prompt as P
from studio import runner
from studio import store as S
from studio import web as studio_web
from studio.settings import DEFAULT_PLAYBOOK, PLAYBOOK_NAME, load_studio_config

T0 = datetime(2026, 9, 10, 12, 0, tzinfo=UTC)
GOOD_PLAYBOOK = "\n\n".join(["# Playbook"] + [f"{h}\n- learned line" for h in L.REQUIRED_SECTIONS])


# ---- fixtures and helpers ----------------------------------------------------------------


@pytest.fixture
def lconn(conn, db_file):
    """The shared pipeline DB with step 3's, step 4's and the studio's tables."""
    publish_store.connect(db_file).close()
    feedback_store.connect(db_file).close()
    S.ensure_tables(conn)
    return conn


@pytest.fixture
def cfg() -> dict[str, Any]:
    c = load_studio_config()
    c["model"], c["effort"] = "writer-model", "max"
    c["learn"].update(
        enabled=True,
        kpi="conversation",
        horizon_hours=48,
        baseline_days=30,
        min_baseline_posts=3,
        smoothing=1.0,
        lean_min_measured=1,
        playbook="auto",
        rewrite_min_new=2,
        rewrite_min_hours=24,
        model="writer-model",
        effort="max",
        max_words=900,
    )
    return c


def post(
    conn,
    item_id: str,
    at: datetime,
    *,
    tweet_id: str | None = None,
    text: str = "An opening line.",
    pictures: int = 0,
    tmp_path: Path | None = None,
) -> int:
    """A draft posted at `at`: its queue row, its first post in step 3's `posts`."""
    draft_id = queue_store.insert_draft(
        conn,
        item_id=item_id,
        model="m",
        draft=Draft(thread=[text], suggested_visual="", why_it_matters="w"),
    )
    for k in range(pictures):
        assert tmp_path is not None
        png = tmp_path / f"d{draft_id}_{k}.png"
        png.write_bytes(b"\x89PNG\r\n\x1a\n")
        queue_store.set_image(conn, draft_id, png, f"card {k}", index=k)
    publish_store.record_post(
        conn,
        draft_id=draft_id,
        text=text,
        kind="thread",
        position=1,
        slot="manual",
        tweet_id=tweet_id or f"9{draft_id:05d}",
        posted_at=at,
    )
    return draft_id


def snap(conn, draft_id: int, at: datetime, **counts: int) -> None:
    """Step 4's snapshot of a draft's first post, taken at `at`."""
    tid = conn.execute(
        "SELECT tweet_id FROM posts WHERE draft_id = ? AND position = 1", (draft_id,)
    ).fetchone()[0]
    feedback_store.record_tweet_metrics(conn, draft_id, TweetMetrics(tid, Metrics(**counts)), at)


def piece(conn, *, angle="catalyst_map", shape="long_post", hook="question", **meta) -> S.Piece:
    pid = S.create_piece(
        conn,
        origin=S.ORIGIN_AUTO,
        topic="",
        cluster_id=None,
        requested_angle="",
        checkpoint=False,
        session_id=f"s-{random.random()}",
        workspace="/nowhere",
        model="writer-model",
        effort="max",
    )
    S.update_piece(
        conn,
        pid,
        stage=S.STAGE_READY,
        angle=angle,
        shape=shape,
        hook_style=hook,
        title=f"Piece {pid}",
        meta=meta or None,
    )
    got = S.get_piece(conn, pid)
    assert got is not None
    return got


def posted_piece(conn, at: datetime, *, likes: int | None = None, link=True, **kw) -> S.Piece:
    """A studio piece posted at `at`, with a snapshot at 50 hours when `likes` is given."""
    pictures = kw.pop("pictures", 0)
    tmp_path = kw.pop("tmp_path", None)
    p = piece(conn, **kw)
    draft_id = post(
        conn,
        queue_store.studio_item_id(p.id),
        at,
        tweet_id=None if link else f"{publish_store.MANUAL_ID_PREFIX}{p.id}-1",
        text=f"Piece {p.id} opens like this.",
        pictures=pictures,
        tmp_path=tmp_path,
    )
    S.update_piece(conn, p.id, draft_id=draft_id)
    if likes is not None and link:
        snap(conn, draft_id, at + timedelta(hours=50), likes=likes)
    return S.get_piece(conn, p.id)


def baseline(conn, values=(10, 20, 30), *, before: datetime = T0) -> None:
    """Drafter posts in the days before `before`, each snapshot at 50 hours."""
    for i, v in enumerate(values):
        at = before - timedelta(days=len(values) - i)
        d = post(conn, f"item-{before:%j}-{i}", at)
        snap(conn, d, at + timedelta(hours=50), likes=v)


# ---- measuring ---------------------------------------------------------------------------


def test_a_posted_piece_is_scored_on_its_snapshot_at_the_horizon(lconn, cfg, tmp_path):
    baseline(lconn)  # median 20
    p = piece(lconn, angle="catalyst_map", shape="thread", hook="hard_number")
    d = post(
        lconn, queue_store.studio_item_id(p.id), T0, text="Opening.", pictures=2, tmp_path=tmp_path
    )
    S.update_piece(lconn, p.id, draft_id=d)
    snap(lconn, d, T0 + timedelta(hours=24), likes=5)  # too young to count
    snap(lconn, d, T0 + timedelta(hours=49), likes=38, replies=1)  # 41
    snap(lconn, d, T0 + timedelta(hours=80), likes=90)  # later than the one compared
    ev = E.measure(lconn, cfg)
    [m] = ev.scored
    assert (m.piece_id, m.draft_id, m.value, m.baseline) == (p.id, d, 41.0, 20.0)
    assert m.relative == pytest.approx(42 / 21)
    assert (m.angle, m.shape, m.hook_style, m.cards) == ("catalyst_map", "thread", "hard_number", 2)
    assert m.metrics["likes"] == 38 and m.metrics["replies"] == 1 and m.source == "x"
    assert m.opening == "Opening." and m.title == f"Piece {p.id}"
    assert (
        m.url == f"https://x.com/i/web/status/{publish_store.list_posts(lconn, d)[0]['tweet_id']}"
    )
    assert len(ev.heads) == 4 and [x.piece.id for x in ev.posted] == [p.id]
    assert ev.waiting == []


def test_a_young_piece_waits_and_a_drafter_post_is_only_baseline(lconn, cfg):
    baseline(lconn)
    young = posted_piece(lconn, T0)
    snap(lconn, young.draft_id, T0 + timedelta(hours=6), likes=99)
    ev = E.measure(lconn, cfg)
    assert ev.measured == [] and [w.piece.id for w in ev.waiting] == [young.id]
    assert len(ev.heads) == 3  # the drafter's posts, not the young piece
    assert all(x.piece.id == young.id for x in ev.posted)


def test_typed_in_numbers_measure_a_piece_posted_by_hand_without_its_link(lconn, cfg):
    baseline(lconn)
    p = posted_piece(lconn, T0, link=False)
    ev = E.measure(lconn, cfg)
    [row] = ev.posted
    assert row.url == "" and row.measured is None  # step 4 never fetches a marker
    S.add_manual_metrics(lconn, p.id, {"likes": 19, "bookmarks": 1})
    [m] = E.measure(lconn, cfg).scored
    assert (m.value, m.source, m.url) == (21.0, "manual", "")
    assert m.relative == pytest.approx(22 / 21)


def test_typed_in_numbers_win_over_the_snapshots(lconn, cfg):
    baseline(lconn)
    p = posted_piece(lconn, T0, likes=10)
    S.add_manual_metrics(lconn, p.id, {"likes": 50})
    [m] = E.measure(lconn, cfg).scored
    assert m.value == 50.0 and m.source == "manual"


def test_a_piece_is_found_by_its_item_id_even_without_its_draft_id(lconn, cfg):
    baseline(lconn)
    p = posted_piece(lconn, T0, likes=20)
    S.update_piece(lconn, p.id, draft_id=None)  # the link on the piece's side is gone
    [m] = E.measure(lconn, cfg).scored
    assert m.piece_id == p.id


def test_too_few_posts_before_a_piece_leave_it_measured_but_unscored(lconn, cfg):
    baseline(lconn, values=(10, 20))
    posted_piece(lconn, T0, likes=20)
    ev = E.measure(lconn, cfg)
    assert len(ev.measured) == 1 and ev.scored == []
    assert E.block(ev, cfg) == ""


def test_a_database_without_posts_says_nothing(conn, cfg):
    S.ensure_tables(conn)
    ev = E.measure(conn, cfg)
    assert (ev.measured, ev.heads, ev.posted) == ([], [], [])


def test_the_editors_hand_edits_are_the_rewrites_evidence_of_taste(lconn):
    p = piece(lconn)
    d = queue_store.insert_draft(
        lconn,
        item_id=queue_store.studio_item_id(p.id),
        model="m",
        draft=Draft(thread=["Before."], suggested_visual="", why_it_matters="w"),
    )
    queue_store.edit(lconn, d, thread=["After, shorter."])
    [edit] = E.edits(lconn)
    assert (edit.piece_id, edit.before, edit.after) == (p.id, "Before.", "After, shorter.")


# ---- the brief ---------------------------------------------------------------------------


def brief(conn, cfg, p: S.Piece, ev: E.Evidence | None) -> P.Brief:
    return runner.build_brief(
        conn,
        cfg,
        p,
        library=A.load_angles(),
        playbook="THE PLAYBOOK",
        today="2026-10-05",
        tzname="UTC",
        evidence=ev,
    )


def test_the_brief_carries_what_x_says_and_a_lean_among_what_is_on_offer(lconn, cfg):
    baseline(lconn)
    for i in range(3):
        posted_piece(
            lconn,
            T0 + timedelta(hours=i),
            likes=20 + 10 * i,
            angle="catalyst_map",
            shape="long_post",
            hook="question",
        )
    ev = E.measure(lconn, cfg)
    new = piece(lconn)
    S.update_piece(lconn, new.id, stage=S.STAGE_RESEARCHING)
    b = brief(lconn, cfg, S.get_piece(lconn, new.id), ev)
    assert b.evidence.startswith("3 studio pieces measured on X so far")
    assert b.lean is not None and b.lean.measured == 3
    # the variety rules come first: the last three pieces' angles are not on offer, the
    # last two hooks are not suggested, and three long posts in a row rule that shape out
    assert b.lean.angle not in ("catalyst_map", "") and b.lean.angle in [
        a.key for a in b.offer.angles
    ]
    assert b.lean.hook_style != "question" and b.lean.shape != "long_post"
    for text in (P.research_prompt(b), P.write_prompt(b)):
        section = text.split("WHAT X SAYS")[1]
        assert b.evidence in section and b.lean.line() in section


def test_the_same_piece_gets_the_same_lean_at_every_stage(lconn, cfg):
    baseline(lconn)
    posted_piece(lconn, T0, likes=30)
    ev = E.measure(lconn, cfg)
    new = piece(lconn)
    leans = {brief(lconn, cfg, new, ev).lean for _ in range(5)}
    assert len(leans) == 1


def test_a_brief_without_evidence_has_no_x_section(lconn, cfg):
    p = piece(lconn)
    b = brief(lconn, cfg, p, None)
    assert (b.evidence, b.lean) == ("", None)
    assert "WHAT X SAYS" not in P.research_prompt(b) + P.write_prompt(b)
    # nothing scored yet: the same
    b = brief(lconn, cfg, p, E.measure(lconn, cfg))
    assert "WHAT X SAYS" not in P.write_prompt(b)


def test_the_context_records_the_lean_each_piece_was_offered(lconn, cfg, monkeypatch):
    baseline(lconn)
    posted_piece(lconn, T0, likes=30)
    monkeypatch.setattr(runner, "make_renderer", lambda c: None)
    ctx = runner.make_context(lconn, cfg)
    new = piece(lconn)
    b = ctx.brief_for(new)
    assert S.get_piece(lconn, new.id).meta["lean"] == b.lean.as_dict()


def test_learning_switched_off_or_broken_never_holds_a_brief_back(lconn, cfg, monkeypatch, caplog):
    baseline(lconn)
    posted_piece(lconn, T0, likes=30)
    cfg["learn"]["enabled"] = False
    assert runner.measure_quietly(lconn, cfg) is None
    cfg["learn"]["enabled"] = True

    def boom(*a, **k):
        raise RuntimeError("tweet_metrics is locked")

    monkeypatch.setattr(E, "measure", boom)
    with caplog.at_level("WARNING"):
        assert runner.measure_quietly(lconn, cfg) is None
    assert "could not read what X says" in caplog.text


# ---- --learn and the rewrite -------------------------------------------------------------


class Rewriter:
    """The one model call of the loop, faked: records each (system, user, kw)."""

    def __init__(self, reply: str | Exception = "") -> None:
        self.reply = reply or json.dumps(
            {"playbook": GOOD_PLAYBOOK, "changelog": ["catalyst maps lead: 2x on 2 posts"]}
        )
        self.calls: list[tuple[str, str, dict[str, Any]]] = []

    def __call__(self, system: str, user: str, **kw: Any) -> str:
        self.calls.append((system, user, kw))
        if isinstance(self.reply, Exception):
            raise self.reply
        return self.reply


@pytest.fixture
def rewriter(monkeypatch, cfg, tmp_path) -> Rewriter:
    fake = Rewriter()
    monkeypatch.setattr(PB, "call_rewriter", fake)
    monkeypatch.setattr(runner, "load_studio_config", lambda *a, **k: cfg)
    monkeypatch.setattr(run_studio, "load_dotenv", lambda *a, **k: False)
    return fake


def scored_pieces(conn, n: int = 2) -> list[S.Piece]:
    baseline(conn)
    return [posted_piece(conn, T0 + timedelta(hours=i), likes=30 + i) for i in range(n)]


def test_learn_rewrites_the_playbook_once_enough_pieces_are_new(lconn, rewriter, tmp_path):
    pieces = scored_pieces(lconn)
    assert run_studio.main(["--learn"]) == 0
    [(system, user, kw)] = rewriter.calls
    assert "at most 900 words" in system
    assert "WHAT X SAYS" in user and "EVERY MEASURED PIECE" in user
    assert (kw["model"], kw["effort"]) == ("writer-model", "max")
    assert kw["timeout"] == 30 * 60
    assert (tmp_path / PLAYBOOK_NAME).read_text(encoding="utf-8") == GOOD_PLAYBOOK + "\n"
    first, learned = sorted(S.playbook_versions(lconn), key=lambda v: v.id)
    assert first.source == S.PLAYBOOK_SEED  # what it replaced, recorded first
    assert learned.source == S.PLAYBOOK_LEARNED and learned.applied
    assert learned.changelog == ["catalyst maps lead: 2x on 2 posts"]
    assert sorted(learned.pieces) == sorted(p.id for p in pieces)
    assert learned.based_on == first.id and learned.evidence.startswith("2 studio pieces")
    # nothing new since: the next run makes no call
    assert run_studio.main(["--learn"]) == 0
    assert len(rewriter.calls) == 1


def test_learn_waits_for_new_pieces_and_time(lconn, rewriter, cfg):
    scored_pieces(lconn, n=1)
    assert run_studio.main(["--learn"]) == 0
    assert rewriter.calls == []  # one new piece, two needed
    assert run_studio.main(["--learn-now"]) == 0
    assert len(rewriter.calls) == 1  # the button rewrites whatever the counts
    posted_piece(lconn, T0 + timedelta(hours=5), likes=40)
    posted_piece(lconn, T0 + timedelta(hours=6), likes=41)
    assert run_studio.main(["--learn"]) == 0
    assert len(rewriter.calls) == 1  # two new pieces, but the last rewrite was just now


def test_learn_with_nothing_scored_makes_no_call_even_when_forced(lconn, rewriter):
    assert run_studio.main(["--learn-now"]) == 0
    assert rewriter.calls == [] and S.playbook_versions(lconn) == []


def test_a_proposal_waits_for_the_editor(lconn, rewriter, cfg, tmp_path):
    cfg["learn"]["playbook"] = "propose"
    scored_pieces(lconn)
    assert run_studio.main(["--learn"]) == 0
    assert not (tmp_path / PLAYBOOK_NAME).exists()  # the sessions still read the seed
    proposal = S.open_proposal(lconn)
    assert proposal is not None and proposal.text == GOOD_PLAYBOOK + "\n"
    assert S.current_playbook_version(lconn).source == S.PLAYBOOK_SEED
    new = PB.apply_proposal(lconn, tmp_path, proposal.id)
    assert (tmp_path / PLAYBOOK_NAME).read_text(encoding="utf-8") == GOOD_PLAYBOOK + "\n"
    applied = S.get_playbook_version(lconn, new)
    assert applied.source == S.PLAYBOOK_LEARNED and applied.based_on == proposal.id
    assert applied.pieces == proposal.pieces and S.open_proposal(lconn) is None
    with pytest.raises(KeyError):
        PB.apply_proposal(lconn, tmp_path, proposal.id)  # applied once only


@pytest.mark.parametrize(
    "reply,why",
    [
        ("not json at all", "not a JSON object"),
        (json.dumps({"playbook": "# Playbook\n\n## Openings\n- x"}), "lacks ## Substance"),
        (claude_cli.ClaudeCliError("CLI timed out after 1800s"), "call failed: CLI timed out"),
    ],
)
def test_a_failed_rewrite_changes_nothing_and_says_why(
    lconn, rewriter, tmp_path, caplog, reply, why
):
    rewriter.reply = reply
    scored_pieces(lconn)
    with caplog.at_level("ERROR"):
        assert run_studio.main(["--learn"]) == 1
    assert why in caplog.text
    assert not (tmp_path / PLAYBOOK_NAME).exists()
    assert S.last_learned(lconn) is None  # so the next run tries again


def test_learn_off_or_playbook_off_rewrites_nothing(lconn, rewriter, cfg, caplog):
    scored_pieces(lconn)
    cfg["learn"]["playbook"] = "off"
    assert run_studio.main(["--learn-now"]) == 0
    cfg["learn"]["playbook"] = "auto"
    cfg["learn"]["enabled"] = False
    with caplog.at_level("INFO"):
        assert run_studio.main(["--learn"]) == 0
    assert rewriter.calls == [] and "learning from X is off" in caplog.text


def test_a_learn_dry_run_prints_the_evidence_and_the_prompt_and_writes_nothing(
    lconn, rewriter, capsys, tmp_path
):
    scored_pieces(lconn)
    assert run_studio.main(["--learn", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("2 studio pieces measured on X so far")
    assert "--- the playbook rewrite's system prompt ---" in out and "EVERY MEASURED PIECE" in out
    assert rewriter.calls == [] and S.playbook_versions(lconn) == []
    assert not (tmp_path / PLAYBOOK_NAME).exists()


def test_a_second_learning_run_finds_the_lock_and_does_nothing(lconn, rewriter, cfg, tmp_path):
    from ops import lock

    scored_pieces(lconn)
    root = tmp_path / "studio_pieces"
    root.mkdir()
    held = lock.acquire(root / runner.LEARN_LOCK_NAME, trust_os_lock=True)
    try:
        assert run_studio.main(["--learn"]) == 0
    finally:
        held.release()
    assert rewriter.calls == []


def test_the_real_rewriter_runs_the_cli_without_tools_and_with_its_own_time_limit(monkeypatch):
    seen = {}

    def run_claude(user, **kw):
        seen.update(kw, user=user)
        return "{}"

    monkeypatch.setattr(claude_cli, "run_claude", run_claude)
    root = {"claude_code": {"binary": "claude", "timeout_seconds": 600}, "other": 1}
    assert (
        PB.call_rewriter("SYS", "USER", model="m", effort="max", root_cfg=root, timeout=1800)
        == "{}"
    )
    assert seen["user"] == "USER" and seen["system"] == "SYS" and seen["model"] == "m"
    assert seen["effort"] == "max" and "tools" not in seen
    assert seen["cfg"]["claude_code"] == {"binary": "claude", "timeout_seconds": 1800}
    assert root["claude_code"]["timeout_seconds"] == 600  # the root config is left alone


# ---- the performance page ----------------------------------------------------------------


@pytest.fixture
def started(monkeypatch) -> list[list[str]]:
    calls: list[list[str]] = []

    def start(steps: list[str], **kw: Any) -> Job:
        calls.append(list(steps))
        return Job(id=f"run{len(calls)}", steps=list(steps), started_at=panel_app._now())

    monkeypatch.setattr(panel_app.JOBS, "start", start)
    return calls


@pytest.fixture
def client(db_file, cfg, monkeypatch, started):
    monkeypatch.setitem(studio_web._starter, "start", panel_app._start_studio)
    monkeypatch.setattr(studio_web, "load_studio_config", lambda *a, **k: cfg)
    return TestClient(panel_app.app, follow_redirects=False)


def flash_of(response) -> str:
    assert response.status_code == 303, response.text
    return parse_qs(urlsplit(response.headers["location"]).query).get("flash", [""])[0]


def test_the_performance_page_before_anything_is_posted(client, lconn):
    body = client.get("/studio/performance").text
    assert "No studio piece has been posted yet" in body
    assert "Nothing scored yet" in body and "No versions yet" in body
    assert 'name="step" value="studio_learn_now"' in body  # the rewrite button, via /runs


def test_the_performance_page_shows_the_numbers_the_arms_and_the_history(
    client, lconn, cfg, tmp_path
):
    scored_pieces(lconn)
    young = posted_piece(lconn, datetime.now(UTC) - timedelta(hours=3), angle="deal_decoder")
    hand = posted_piece(lconn, T0 + timedelta(hours=9), link=False, angle="the_race")
    PB.save(
        lconn, tmp_path, GOOD_PLAYBOOK, source=S.PLAYBOOK_EDITOR, changelog=["shorter openings"]
    )
    body = client.get("/studio/performance").text
    assert "2 studio pieces measured on X so far" in body  # the text sessions read
    assert "1.5&times;" in body  # likes 30 and 31 against a median of 20
    assert "compared at 48 hours: waiting until then" in body  # the young one
    assert "posted by hand without its link" in body
    assert f'action="/studio/performance/{hand.id}/link"' in body
    assert f'action="/studio/performance/{young.id}/link"' not in body
    assert "untried" in body and "Suggested" in body
    assert "shorter openings" in body and "in use" in body
    assert "put this version back" in body  # the seed, recorded before the edit


def test_typed_in_numbers_are_saved_and_checked(client, lconn):
    p = posted_piece(lconn, T0, link=False)
    r = client.post(
        f"/studio/performance/{p.id}/metrics", data={"likes": "1,204", "replies": "7", "quotes": ""}
    )
    assert flash_of(r) == f"numbers saved for piece {p.id}; they count from now on"
    [(snapshot)] = S.manual_metrics(lconn)[p.id]
    assert snapshot["counts"]["likes"] == 1204 and snapshot["counts"]["replies"] == 7
    assert snapshot["counts"]["quotes"] == 0
    assert flash_of(client.post(f"/studio/performance/{p.id}/metrics", data={"likes": "-3"})) == (
        "likes: a whole number, not '-3'"
    )
    assert flash_of(client.post(f"/studio/performance/{p.id}/metrics", data={})) == (
        "type at least one of the numbers X shows"
    )
    assert client.post("/studio/performance/999/metrics", data={"likes": "1"}).status_code == 404


def test_the_link_of_a_piece_posted_by_hand_lets_step_4_measure_it(client, lconn):
    p = posted_piece(lconn, T0, link=False)
    url = "https://x.com/acct/status/1844000000000000001?s=20"
    r = client.post(f"/studio/performance/{p.id}/link", data={"url": url})
    assert flash_of(r) == (
        f"post 1844000000000000001 linked to draft {p.draft_id}: the next feedback snapshot "
        "fetches its numbers"
    )
    [head] = [h for h in S.fetch_posted_heads(lconn) if h.draft_id == p.draft_id]
    assert head.tweet_id == "1844000000000000001" and head.measurable
    # step 4 now asks X for it
    assert any(r.tweet_id == head.tweet_id for r in feedback_store.fetch_posted(None, conn=lconn))
    # a real id is never overwritten; the same link again is fine
    again = client.post(f"/studio/performance/{p.id}/link", data={"url": url})
    assert "linked" in flash_of(again)
    other = client.post(
        f"/studio/performance/{p.id}/link",
        data={"url": "https://x.com/a/status/1844000000000000002"},
    )
    assert flash_of(other) == (
        f"not linked: draft {p.draft_id}'s first post already has its X id (1844000000000000001)"
    )
    bad = client.post(f"/studio/performance/{p.id}/link", data={"url": "x.com/acct"})
    assert flash_of(bad).startswith("not linked: that is not a link to a post on X")


def test_a_post_already_logged_for_another_draft_is_not_linked_twice(lconn):
    a = posted_piece(lconn, T0, link=False)
    b = posted_piece(lconn, T0, link=True)
    tid = publish_store.list_posts(lconn, b.draft_id)[0]["tweet_id"]
    with pytest.raises(ValueError, match=f"post {tid} is already logged, for draft {b.draft_id}"):
        publish_store.set_head_tweet(lconn, a.draft_id, tid)
    with pytest.raises(ValueError, match="has no posted first post"):
        publish_store.set_head_tweet(lconn, 4242, "https://x.com/a/status/12345678")


def test_a_version_is_put_back_as_a_new_one(client, lconn, tmp_path):
    PB.save(lconn, tmp_path, "# Playbook\n\nOne.\n", source=S.PLAYBOOK_EDITOR)
    PB.save(lconn, tmp_path, "# Playbook\n\nTwo.\n", source=S.PLAYBOOK_EDITOR)
    seed, one, two = sorted(S.playbook_versions(lconn), key=lambda v: v.id)
    r = client.post(f"/studio/playbook/versions/{one.id}/revert")
    assert (
        flash_of(r)
        == f"version {one.id} is back, as version {two.id + 1}; the next session reads it"
    )
    assert (tmp_path / PLAYBOOK_NAME).read_text(encoding="utf-8") == "# Playbook\n\nOne.\n"
    back = S.current_playbook_version(lconn)
    assert back.source == S.PLAYBOOK_REVERT and back.based_on == one.id
    assert client.post("/studio/playbook/versions/999/revert").status_code == 404


def test_a_proposal_is_applied_from_the_page_and_a_stale_one_is_refused(client, lconn, tmp_path):
    PB.ensure_seeded(lconn, tmp_path)
    proposal = PB.save(lconn, tmp_path, GOOD_PLAYBOOK, source=S.PLAYBOOK_PROPOSAL, pieces=[1])
    body = client.get("/studio/performance").text
    assert "waiting for you to apply it" in body
    assert f'action="/studio/playbook/versions/{proposal}/apply"' in body
    assert "learned the proposal" not in client.get("/studio/playbook").text
    assert f"proposed version {proposal}" in client.get("/studio/playbook").text
    r = client.post(f"/studio/playbook/versions/{proposal}/apply")
    assert (
        flash_of(r)
        == f"proposal {proposal} applied as version {proposal + 1}; the next session reads it"
    )
    assert f"applied as version {proposal + 1}" in client.get("/studio/performance").text
    # a proposal overtaken by the editor's own save cannot be applied any more
    stale = PB.save(lconn, tmp_path, GOOD_PLAYBOOK + "\nx\n", source=S.PLAYBOOK_PROPOSAL)
    PB.save(lconn, tmp_path, "# Playbook\n\nMine.\n", source=S.PLAYBOOK_EDITOR)
    r = client.post(f"/studio/playbook/versions/{stale}/apply")
    assert "not the open proposal any more" in flash_of(r)
    assert "overtaken by a later version" in client.get("/studio/performance").text


def test_without_the_panel_a_link_cannot_be_added(client, lconn, monkeypatch):
    monkeypatch.setitem(studio_web._starter, "add_link", None)
    p = posted_piece(lconn, T0, link=False)
    r = client.post(
        f"/studio/performance/{p.id}/link", data={"url": "https://x.com/a/status/123456"}
    )
    assert flash_of(r) == "adding a link needs the control panel"
    assert "add the post's link" not in client.get("/studio/performance").text


# ---- the dashboard's arithmetic ------------------------------------------------------------


def test_each_version_shows_what_it_changed_against_the_one_in_use_before_it(lconn, tmp_path):
    PB.save(lconn, tmp_path, "# Playbook\n\nA\nB\n", source=S.PLAYBOOK_EDITOR)
    proposal = PB.save(lconn, tmp_path, "# Playbook\n\nA\nC\n", source=S.PLAYBOOK_PROPOSAL)
    PB.apply_proposal(lconn, tmp_path, proposal)
    rows = D.version_rows(S.playbook_versions(lconn), S.open_proposal(lconn))
    applied, prop, edited, seed = rows
    assert applied.current and applied.diff == "@@ -3,2 +3,2 @@\n A\n-B\n+C"
    assert prop.status == f"applied as version {applied.version.id}" and not prop.current
    assert prop.diff == applied.diff  # against the playbook in use when it was proposed
    assert seed.diff == "" and not seed.current  # the first has nothing before it
    assert edited.diff.startswith("@@")


def test_the_lean_shares_cover_every_value_and_untried_ones_show_as_such(lconn, cfg):
    scored_pieces(lconn)
    ev = E.measure(lconn, cfg)
    tables = {t.arm: t for t in D.arm_tables(ev, cfg, angles=list(A.load_angles()))}
    angle_rows = {r.value: r for r in tables["angle"].rows}
    assert set(angle_rows) == set(A.load_angles())
    assert angle_rows["catalyst_map"].n == 2 and angle_rows["catalyst_map"].times > 1
    assert sum(r.share for r in angle_rows.values()) == pytest.approx(1.0)
    untried = [r for r in angle_rows.values() if r.n == 0]
    assert untried and all(r.times is None and r.share > 0 for r in untried)
    assert all(r.share is None for r in tables["cards"].rows)  # the lean never draws cards
    assert {r.value for r in tables["shape"].rows} == set(A.SHAPES)


def test_the_learning_state_counts_what_is_new_and_when_the_next_rewrite_may_run(lconn, cfg):
    scored_pieces(lconn)
    ev = E.measure(lconn, cfg)
    now = T0 + timedelta(days=5)
    state = D.learn_state(ev, cfg, None, now)
    assert (state.new_pieces, state.due, state.next_after) == (2, True, None)
    vid = S.add_playbook_version(
        lconn, text=GOOD_PLAYBOOK, source=S.PLAYBOOK_LEARNED, pieces=[ev.scored[0].piece_id]
    )
    last = S.get_playbook_version(lconn, vid)
    state = D.learn_state(ev, cfg, last, datetime.fromisoformat(last.created_at))
    assert state.new_pieces == 1 and not state.due
    assert state.next_after == datetime.fromisoformat(last.created_at) + timedelta(hours=24)


def test_the_seed_playbook_has_the_sections_a_rewrite_must_keep_but_cards():
    seed = DEFAULT_PLAYBOOK.read_text(encoding="utf-8")
    missing = [s for s in L.REQUIRED_SECTIONS if s not in seed]
    assert missing == ["## Cards"]
