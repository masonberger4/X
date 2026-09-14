"""run_draft.py with the swarm ON: variants and the run row are recorded and the jury's pick
becomes the pending draft."""

import json

from approval_queue import store
from swarm import store as swarm_store
from swarm.prompts import JUDGE_SYSTEM, THREAD_JUDGE_SYSTEM
from tests.conftest import URL, seed_item
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
            "thread": ["control one", "control two", f"control three {URL}"],
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
