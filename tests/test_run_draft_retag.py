"""`run_draft.py --retag`: bring current drafts under hard rule 11 through the revise path."""

import run_draft
from approval_queue import store
from draft.drafter import DraftResult
from draft.schema import SHAPE_LONG, Draft, validate_output
from draft.tags import Handle
from tests.conftest import seed_item

HANDLES = [Handle(handle="Merck", name="Merck")]


def _out(*posts):
    return {
        "thread": list(posts),
        "why_it_matters": "w",
        "claims_to_verify": [{"claim": "Merck leads", "confidence": "low"}],
        "suggested_visual": "v",
        "chart": {"title": "t", "labels": ["a", "b"], "values": [1, 2], "unit": ""},
        "table": None,
    }


_n = [0]


def _seed(conn, *posts, status=store.STATUS_PENDING):
    _n[0] += 1
    item_id = f"i{_n[0]}"
    cid = seed_item(conn, item_id)
    return store.insert_draft(
        conn,
        item_id=item_id,
        cluster_id=cid,
        model="m",
        draft=validate_output(_out(*posts)),
        status=status,
    )


def test_retag_revises_only_offending_drafts(conn, monkeypatch):
    bad = _seed(conn, "Merck in NCT04487080", "two", "three", "four")
    good = _seed(conn, "@Merck in #NCT04487080", "two", "three", "four")
    approved = _seed(conn, "Merck again", "two", "three", "four", status=store.STATUS_APPROVED)
    monkeypatch.setattr(run_draft, "story_handles", lambda **kw: HANDLES)
    calls = []

    def fake_revise(*, current, instructions, **kw):
        calls.append((current.thread[0], instructions))
        fixed = validate_output(_out("@Merck in #NCT04487080", "two", "three", "four"))
        return DraftResult(draft=fixed, model="m2", attempts=1, flagged_numbers=[])

    monkeypatch.setattr(run_draft, "revise_item", fake_revise)
    monkeypatch.setattr(run_draft.images, "attach_chart", lambda *a, **k: None)

    assert run_draft.retag_drafts(conn, dry_run=True) == 0 and calls == []
    assert run_draft.retag_drafts(conn) == 2
    assert sorted(c[0] for c in calls) == ["Merck again", "Merck in NCT04487080"]
    assert all(c[1] == run_draft.RETAG_INSTRUCTIONS for c in calls)
    for did in (bad, approved):
        row = store.get_draft(conn, did)
        assert row.draft.thread[0] == "@Merck in #NCT04487080"
        decisions = store.list_decisions(conn, did)
        assert decisions[-1]["action"] == store.ACTION_REVISE
        assert decisions[-1]["note"] == run_draft.RETAG_NOTE
    assert store.get_draft(conn, approved).status == store.STATUS_APPROVED
    assert store.get_draft(conn, good).draft.thread[0] == "@Merck in #NCT04487080"
    assert not store.list_decisions(conn, good)
    assert run_draft.retag_drafts(conn) == 0  # everything complies now


def test_retag_leaves_a_studio_piece_alone(conn, monkeypatch):
    """A studio piece names drugs and companies the studio's way, so it breaks rule 11 as
    the drafter reads it; the drafter's rewrite would replace its long post and drop its
    cards. It is revised in the studio (the queue's Revise refuses it the same way)."""
    studio = store.insert_draft(
        conn,
        item_id=store.studio_item_id(7),
        model="writer-model (studio)",
        draft=Draft(
            thread=["Merck in NCT04487080, a long post."],
            suggested_visual="card 1",
            why_it_matters="w",
            shape=SHAPE_LONG,
            max_chars=25000,
        ),
    )
    drafted = _seed(conn, "Merck in NCT04487080", "two", "three", "four")
    monkeypatch.setattr(run_draft, "story_handles", lambda **kw: HANDLES)
    assert run_draft.retag_problems(store.get_draft(conn, studio))  # it does break rule 11
    calls = []

    def fake_revise(*, current, instructions, **kw):
        calls.append(current.thread[0])
        fixed = validate_output(_out("@Merck in #NCT04487080", "two", "three", "four"))
        return DraftResult(draft=fixed, model="m2", attempts=1, flagged_numbers=[])

    monkeypatch.setattr(run_draft, "revise_item", fake_revise)
    monkeypatch.setattr(run_draft.images, "attach_chart", lambda *a, **k: None)

    assert run_draft.retag_drafts(conn) == 1
    assert calls == ["Merck in NCT04487080"]
    assert store.get_draft(conn, drafted).draft.thread[0] == "@Merck in #NCT04487080"
    row = store.get_draft(conn, studio)
    assert row.draft.thread == ["Merck in NCT04487080, a long post."]
    assert row.model == "writer-model (studio)" and not store.list_decisions(conn, studio)


def test_retag_cli_flag(db_file, monkeypatch):
    real = store.connect
    monkeypatch.setattr(store, "connect", lambda *a, **k: real(db_file))
    seen = []
    monkeypatch.setattr(run_draft, "retag_drafts", lambda conn, dry_run: seen.append(dry_run) or 0)
    assert run_draft.main(["--retag", "--dry-run"]) == 0
    assert seen == [True]
    assert "--retag" in run_draft.__doc__
