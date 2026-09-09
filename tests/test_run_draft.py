import json

from approval_queue import store
from tests.conftest import URL, seed_item


def good_json(single_post=f"ORR 88% in 97 patients. {URL}"):
    return json.dumps(
        {
            "single_post": single_post,
            "thread": ["a", "b", f"c {URL}"],
            "suggested_visual": "v",
            "why_it_matters": "w",
            "claims_to_verify": [],
        }
    )


def test_run_draft_drafts_only_undrafted_candidates(conn, monkeypatch):
    import run_draft

    seed_item(conn, "new", total=9.0)
    seed_item(conn, "done", total=9.0)
    seed_item(conn, "low", total=2.0)
    store.insert_draft(
        conn,
        item_id="done",
        model="m",
        draft=store.Draft("x", ["a", "b", "c"], "", ""),
    )
    calls = []

    def fake_call(system, user, model):
        calls.append(user)
        return good_json()

    monkeypatch.setattr(
        run_draft,
        "draft_item",
        lambda **kw: __import__("draft.drafter").drafter.draft_item(
            call=fake_call, sleep=lambda s: None, **kw
        ),
    )
    assert run_draft.main(["--min-score", "7"]) == 0
    assert len(calls) == 1 and "Title new" in calls[0]
    row = [r for r in store.list_drafts(conn) if r.item_id == "new"]
    assert len(row) == 1 and row[0].status == "pending"

    # second run: nothing left to draft
    calls.clear()
    run_draft.main(["--min-score", "7"])
    assert calls == []


def test_run_draft_stores_hard_rule_failures_as_failed(conn, monkeypatch):
    import run_draft
    from draft import drafter

    seed_item(conn, "bad", total=9.0)
    monkeypatch.setattr(
        run_draft,
        "draft_item",
        lambda **kw: drafter.draft_item(
            call=lambda s, u, m: good_json("no url at all"),
            sleep=lambda s: None,
            max_attempts=2,
            **kw,
        ),
    )
    run_draft.main([])
    failed = store.list_drafts(conn, store.STATUS_FAILED)
    assert len(failed) == 1 and "missing the primary source URL" in failed[0].rejection_reason
    assert store.list_drafts(conn) == []


def test_run_draft_dry_run_makes_no_calls(conn, monkeypatch):
    import run_draft

    seed_item(conn, "x", total=9.0)
    monkeypatch.setattr(run_draft, "draft_item", lambda **kw: (_ for _ in ()).throw(AssertionError))
    run_draft.main(["--dry-run"])
    assert store.list_drafts(conn) == []


def test_run_draft_without_step1_tables_fails_cleanly(tmp_path, monkeypatch, caplog):
    import run_draft

    monkeypatch.setenv("DB_PATH", str(tmp_path / "empty.db"))
    assert run_draft.main([]) == 1
    assert "run step 1" in caplog.text
