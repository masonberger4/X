"""run_draft.py with the swarm ON: variants and the run row are recorded and the jury's pick
becomes the pending draft."""

import json

from approval_queue import store
from swarm import store as swarm_store
from swarm.prompts import JUDGE_SYSTEM, THREAD_JUDGE_SYSTEM
from tests.conftest import seed_item
from tests.test_swarm_engine import FakeModel

CFG = {
    "enabled": True,
    "model": "cheap",
    "assembler_model": "",
    "fan_out": 2,
    "layers": 1,
    "judge_votes": 3,
    "max_similarity": 0.99,
    "control": {"enabled": True},
}


def _control_json():
    return json.dumps(
        {
            "thread": ["control one", "control two", "control three"],
            "suggested_visual": "v",
            "why_it_matters": "w",
            "claims_to_verify": [],
            "chart": {
                "title": "Outcomes",
                "labels": ["ORR", "PFS"],
                "values": [88, 14.6],
                "unit": "",
            },
        }
    )


def _wire(monkeypatch, fake, jury_pick):
    """Route the swarm's calls and the control's call to fakes; jury answers with jury_pick."""
    import run_draft
    from draft import drafter
    from swarm import engine

    monkeypatch.setattr(run_draft, "load_swarm_config", lambda *a, **k: CFG)
    monkeypatch.setattr(engine, "call_anthropic", fake)
    monkeypatch.setattr(
        run_draft,
        "draft_item",
        lambda **kw: drafter.draft_item(
            call=lambda s, u, m: _control_json(), sleep=lambda s: None, **kw
        ),
    )

    class Jury(FakeModel):
        def __call__(self, system, user, model):
            if system == THREAD_JUDGE_SYSTEM:
                self.calls.append((system, user, model))
                a_is_swarm = "THREAD A:\n  1. hook" in user
                want_a = (jury_pick == "swarm") == a_is_swarm
                return json.dumps({"winner": "A" if want_a else "B"})
            return super().__call__(system, user, model)

    jury = Jury(bad_every=fake.bad_every)
    monkeypatch.setattr(engine, "call_anthropic", jury)
    # run_swarm/compare take call=call_anthropic as a default bound at import: patch the
    # engine's default by wrapping the functions
    orig_run, orig_cmp = engine.run_swarm, engine.compare
    monkeypatch.setattr(
        engine, "run_swarm", lambda *a, **k: orig_run(*a, call=jury, sleep=lambda s: None, **k)
    )
    monkeypatch.setattr(engine, "compare", lambda *a, **k: orig_cmp(*a, call=jury, **k))
    return jury


def test_swarm_wins_and_is_stored_as_the_pending_draft(conn, monkeypatch):
    import run_draft

    seed_item(conn, "s1", total=9.0)
    jury = _wire(monkeypatch, FakeModel(), jury_pick="swarm")
    assert run_draft.main(["--min-score", "7"]) == 0
    drafts = store.list_drafts(conn)
    assert len(drafts) == 1 and drafts[0].status == "pending"
    assert drafts[0].model == "swarm:cheap"
    assert drafts[0].draft.thread[0].startswith("hook ")
    runs = swarm_store.list_runs(conn)
    assert len(runs) == 1 and runs[0].winner == "swarm" and runs[0].draft_id == drafts[0].id
    assert runs[0].calls == len(jury.calls)
    variants = swarm_store.list_variants(conn, runs[0].id)
    assert [v["role"] for v in variants] == ["swarm", "control"]
    assert all(v["ok"] == 1 for v in variants)
    # the genome owns its topology (default-6: fan_out 6, layers 2); the tournament runs
    # over the final layer's 6 candidates, 5 matches per slot. The config's fan_out and
    # layers are only a fallback for a genome row without them.
    assert sum(c[0] == JUDGE_SYSTEM for c in jury.calls) == 6 * 5
    assert sum(c[0] == THREAD_JUDGE_SYSTEM for c in jury.calls) == 3
    run_row = conn.execute("SELECT designer_id FROM swarm_runs").fetchone()
    assert run_row[0] is not None  # a designer was picked for the picture


def test_control_wins_keeps_the_strong_drafters_text(conn, monkeypatch):
    import run_draft

    seed_item(conn, "s2", total=9.0)
    _wire(monkeypatch, FakeModel(), jury_pick="control")
    assert run_draft.main(["--min-score", "7"]) == 0
    d = store.list_drafts(conn)[0]
    assert d.draft.thread[0] == "control one" and not d.model.startswith("swarm:")
    assert swarm_store.list_runs(conn)[0].winner == "control"


def test_swarm_failure_falls_back_to_control(conn, monkeypatch):
    import run_draft

    seed_item(conn, "s3", total=9.0)
    _wire(monkeypatch, FakeModel(bad_every=1), jury_pick="swarm")
    assert run_draft.main(["--min-score", "7"]) == 0
    d = store.list_drafts(conn)[0]
    assert d.status == "pending" and d.draft.thread[0] == "control one"
    run = swarm_store.list_runs(conn)[0]
    assert run.winner == "control"
    variants = swarm_store.list_variants(conn, run.id)
    assert variants[0]["ok"] == 0 and "hook" in variants[0]["problems"]


def test_no_swarm_flag_skips_everything(conn, monkeypatch):
    import run_draft

    seed_item(conn, "s4", total=9.0)
    jury = _wire(monkeypatch, FakeModel(), jury_pick="swarm")
    assert run_draft.main(["--min-score", "7", "--no-swarm"]) == 0
    assert jury.calls == []
    assert store.list_drafts(conn)[0].draft.thread[0] == "control one"
    assert "swarm_runs" not in {
        r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }


def test_run_records_the_format_and_both_variants_write_to_it(conn, monkeypatch):
    import run_draft

    seed_item(conn, "s5", total=9.0)
    _wire(monkeypatch, FakeModel(), jury_pick="swarm")
    assert run_draft.main(["--min-score", "7"]) == 0
    row = conn.execute("SELECT format_id, log_json FROM swarm_runs").fetchone()
    assert row[0] is not None
    assert json.loads(row[1])["format"]["shape"] == "thread"
    d = store.list_drafts(conn)[0]
    assert d.draft.wanted_visuals == 1 and d.draft.shape == "thread"
    assert conn.execute("SELECT format_json FROM drafts").fetchone()[0]


# --- jury: human -------------------------------------------------------------


def _wire_human(monkeypatch):
    import run_draft

    jury = _wire(monkeypatch, FakeModel(), jury_pick="swarm")
    monkeypatch.setattr(run_draft, "load_swarm_config", lambda *a, **k: {**CFG, "jury": "human"})
    return jury


def _choosing(conn):
    rows = store.list_drafts(conn, store.STATUS_CHOOSING)
    assert len(rows) == 1
    return rows[0]


def test_human_jury_stores_one_draft_awaiting_the_pick_and_calls_no_judge(conn, monkeypatch):
    import run_draft

    seed_item(conn, "h1", total=9.0)
    jury = _wire_human(monkeypatch)
    assert run_draft.main(["--min-score", "7"]) == 0
    assert store.list_drafts(conn) == []  # nothing pending: verify has nothing to check yet
    d = _choosing(conn)
    assert not any(c[0] == THREAD_JUDGE_SYSTEM for c in jury.calls)
    choice = store.get_choice(conn, d.id)
    roles = {choice.a.role, choice.b.role}
    assert roles == {"swarm", "control"}
    control = choice.a if choice.a.role == "control" else choice.b
    assert control.draft.thread[0] == "control one"
    assert control.draft.chart is not None and control.draft.chart.labels == ["ORR", "PFS"]
    run = swarm_store.list_runs(conn)[0]
    assert run.winner is None and run.draft_id == d.id


def test_picking_the_control_makes_it_the_pending_draft_and_records_the_winner(conn, monkeypatch):
    import run_draft
    from approval_queue import choosing

    seed_item(conn, "h2", total=9.0)
    _wire_human(monkeypatch)
    run_draft.main(["--min-score", "7"])
    d = _choosing(conn)
    choice = store.get_choice(conn, d.id)
    label = "A" if choice.a.role == "control" else "B"
    picked = choosing.pick(conn, d.id, label)
    assert picked.role == "control"
    row = store.get_draft(conn, d.id)
    assert row.status == "pending" and row.draft.thread[0] == "control one"
    assert row.draft.chart is not None and not row.model.startswith("swarm:")
    assert store.get_choice(conn, d.id) is None
    run = swarm_store.list_runs(conn)[0]
    assert run.winner == "control"
    log = json.loads(conn.execute("SELECT log_json FROM swarm_runs").fetchone()[0])
    assert log["jury"] == {"human": "control"}
    assert not list(store.image_dir().glob(f"choice_{d.id}_*.png"))  # previews gone


def test_picking_the_swarm_keeps_its_text(conn, monkeypatch):
    import run_draft
    from approval_queue import choosing

    seed_item(conn, "h3", total=9.0)
    _wire_human(monkeypatch)
    run_draft.main(["--min-score", "7"])
    d = _choosing(conn)
    choice = store.get_choice(conn, d.id)
    label = "A" if choice.a.role == "swarm" else "B"
    choosing.pick(conn, d.id, label)
    row = store.get_draft(conn, d.id)
    assert row.status == "pending" and row.draft.thread[0].startswith("hook ")
    assert swarm_store.list_runs(conn)[0].winner == "swarm"


def test_the_queue_pick_page_is_blind_and_the_pick_route_moves_on(conn, monkeypatch):
    from fastapi.testclient import TestClient

    import run_draft
    from approval_queue.app import app

    seed_item(conn, "h4", total=9.0)
    seed_item(conn, "h5", total=9.0)
    _wire_human(monkeypatch)
    drawn = []
    orig_draw = run_draft.draw_genomes

    def same_format(*a, **k):  # the fakes write the first seed format only
        drawn.append(drawn[0] if drawn else orig_draw(*a, **k))
        return drawn[-1]

    monkeypatch.setattr(run_draft, "draw_genomes", same_format)
    run_draft.main(["--min-score", "7"])
    first, second = store.list_drafts(conn, store.STATUS_CHOOSING)
    client = TestClient(app)
    page = client.get(f"/choose/{first.id}").text
    assert "Pick A" in page and "Pick B" in page
    assert "swarm:" not in page and "strong drafter" not in page  # blind: no roles or models
    assert "control one" in page  # both texts are on the page
    # the draft page, approve and revise refuse a draft awaiting its pick
    assert client.get(f"/drafts/{first.id}", follow_redirects=False).headers["location"] == (
        f"/choose/{first.id}"
    )
    assert client.post(f"/drafts/{first.id}/approve").status_code == 409
    assert "waiting for your A/B pick" in client.get("/queue").text
    r = client.post(f"/choose/{first.id}", data={"side": "A"}, follow_redirects=True)
    assert r.status_code == 200 and f"/choose/{second.id}" in str(r.url)
    assert "you picked A" in r.text
    assert store.get_draft(conn, first.id).status == "pending"
    r = client.post(f"/choose/{second.id}/reject", data={"note": "meh"}, follow_redirects=True)
    assert store.get_draft(conn, second.id).status == "rejected"
    assert "Nothing to pick right now" in r.text and "both rejected" in r.text
    runs = {r.draft_id: r.winner for r in swarm_store.list_runs(conn)}
    assert runs[first.id] in ("swarm", "control") and runs[second.id] is None


def test_a_refused_control_loses_to_the_swarm(conn, monkeypatch):
    """The control's prompt is refused (every run alike): it counts as a failed variant,
    so the swarm's draft still wins."""
    import claude_cli
    import run_draft

    seed_item(conn, "s5", total=9.0)
    _wire(monkeypatch, FakeModel(), jury_pick="control")

    def refused(**kw):
        raise claude_cli.ClaudeCliRefused("CLI exited 1: safeguards flagged this message")

    monkeypatch.setattr(run_draft, "draft_item", refused)
    assert run_draft.main(["--min-score", "7"]) == 0
    [d] = store.list_drafts(conn)
    assert d.status == "pending" and d.model == "swarm:cheap"
    run = swarm_store.list_runs(conn)[0]
    assert run.winner == "swarm" and run.draft_id == d.id
    control = swarm_store.list_variants(conn, run.id)[1]
    assert control["role"] == "control" and control["ok"] == 0
    assert run_draft.REFUSED in control["problems"]


def test_a_refused_control_and_a_failed_swarm_store_the_story_failed(conn, monkeypatch):
    import claude_cli
    import run_draft

    seed_item(conn, "s6", total=9.0)
    _wire(monkeypatch, FakeModel(bad_every=1), jury_pick="swarm")
    monkeypatch.setattr(
        run_draft,
        "draft_item",
        lambda **kw: (_ for _ in ()).throw(claude_cli.ClaudeCliRefused("safeguards flagged")),
    )
    assert run_draft.main(["--min-score", "7"]) == 0
    [failed] = store.list_drafts(conn, store.STATUS_FAILED)
    assert failed.rejection_reason.startswith(f"{run_draft.REFUSED}: safeguards flagged")
    assert store.list_drafts(conn) == []
