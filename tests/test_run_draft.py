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
    run_draft.main(["--min-score", "7"])
    failed = store.list_drafts(conn, store.STATUS_FAILED)
    assert len(failed) == 1 and "missing the primary source URL" in failed[0].rejection_reason
    assert store.list_drafts(conn) == []


def test_run_draft_dry_run_makes_no_calls(conn, monkeypatch):
    import run_draft

    seed_item(conn, "x", total=9.0)
    monkeypatch.setattr(run_draft, "draft_item", lambda **kw: (_ for _ in ()).throw(AssertionError))
    run_draft.main(["--dry-run", "--min-score", "7"])
    assert store.list_drafts(conn) == []


def test_run_draft_without_step1_tables_fails_cleanly(tmp_path, monkeypatch, caplog):
    import run_draft

    monkeypatch.setenv("DB_PATH", str(tmp_path / "empty.db"))
    assert run_draft.main([]) == 1
    assert "run step 1" in caplog.text


# --- step 7: examples end to end -----------------------------------------------------


def _run_with_capture(monkeypatch, argv):
    """Run run_draft.main with a fake API call; return the system prompts it saw."""
    import run_draft
    from draft import drafter

    systems = []

    def fake_call(system, user, model):
        systems.append(system)
        return good_json()

    monkeypatch.setattr(
        run_draft,
        "draft_item",
        lambda **kw: drafter.draft_item(call=fake_call, sleep=lambda s: None, **kw),
    )
    assert run_draft.main(argv) == 0
    return systems


def test_run_draft_feeds_recent_edits_into_the_prompt_and_records_them(conn, monkeypatch, caplog):
    import logging

    from draft.prompt import HARD_RULES

    caplog.set_level(logging.INFO)

    seed_item(conn, "old", total=9.0, hours_ago=30)
    old = store.insert_draft(
        conn,
        item_id="old",
        model="m",
        draft=store.Draft(f"A game-changer! ORR 88%. {URL}", ["a", "b", f"c {URL}"], "", ""),
    )
    before = f"A game-changer! ORR 88%. {URL}"
    after = f"ORR 88% in a single-arm study. The sequencing question is open. {URL}"
    edit_id = store.edit(
        conn,
        old,
        single_post=after,
        thread=["a", "b", f"c {URL}"],
        note="less hype",
        category="voice",
    )
    reject_target = store.insert_draft(
        conn,
        item_id="rej",
        model="m",
        draft=store.Draft(f"Meh {URL}", ["a", "b", f"c {URL}"], "", ""),
    )
    reject_id = store.reject(conn, reject_target, note="not news")
    seed_item(conn, "new", total=9.0)

    systems = _run_with_capture(monkeypatch, ["--min-score", "7"])
    assert len(systems) == 1
    system = systems[0]
    assert "=== RECENT HUMAN EDITS ===" in system
    assert "BEFORE (model):\n" + before in system
    assert "AFTER (human):\n" + after in system
    assert "WHY: less hype" in system
    assert "=== RECENTLY REJECTED ===" in system and "REASON: not news" in system
    assert system.index("=== RECENT HUMAN EDITS ===") < system.index(HARD_RULES)
    assert "voice examples: 1 edits, 1 rejections (last 60 days)" in caplog.text

    new = [r for r in store.list_drafts(conn) if r.item_id == "new"][0]
    rows = store.list_examples(conn, new.id)
    assert [(r["decision_id"], r["kind"]) for r in rows] == [
        (edit_id, "edit"),
        (reject_id, "rejection"),
    ]
    # the run's own new draft was not shown to itself
    assert store.list_examples(conn, old) == []


def test_run_draft_no_examples_flag_sends_plain_prompt(conn, monkeypatch, caplog):
    import logging

    caplog.set_level(logging.INFO)
    seed_item(conn, "old", total=9.0, hours_ago=30)
    old = store.insert_draft(
        conn,
        item_id="old",
        model="m",
        draft=store.Draft(f"A game-changer! ORR 88%. {URL}", ["a", "b", f"c {URL}"], "", ""),
    )
    store.edit(
        conn,
        old,
        single_post=f"ORR 88% in a single-arm study. Sequencing is open. {URL}",
        thread=["a", "b", f"c {URL}"],
        note="less hype",
    )
    seed_item(conn, "new", total=9.0)
    systems = _run_with_capture(monkeypatch, ["--min-score", "7", "--no-examples"])
    assert len(systems) == 1 and "RECENT HUMAN EDITS" not in systems[0]
    new = [r for r in store.list_drafts(conn) if r.item_id == "new"][0]
    assert store.list_examples(conn, new.id) == []
    assert conn.execute("SELECT COUNT(*) FROM draft_examples").fetchone()[0] == 0
    assert "disabled by --no-examples" in caplog.text


def test_run_draft_examples_disabled_in_config(conn, monkeypatch):
    import run_draft

    seed_item(conn, "new", total=9.0)
    monkeypatch.setattr(
        run_draft, "load_draft_config", lambda: {"examples": {"enabled": False}, "report": {}}
    )
    called = []
    monkeypatch.setattr(run_draft, "build_examples", lambda *a: called.append(1))
    systems = _run_with_capture(monkeypatch, ["--min-score", "7"])
    assert len(systems) == 1 and called == []


def test_run_draft_dry_run_reports_examples_without_calls(conn, monkeypatch, caplog):
    import logging

    import run_draft

    caplog.set_level(logging.INFO)

    seed_item(conn, "x", total=9.0)
    monkeypatch.setattr(run_draft, "draft_item", lambda **kw: (_ for _ in ()).throw(AssertionError))
    assert run_draft.main(["--dry-run", "--min-score", "7"]) == 0
    assert "examples block is 0 chars" in caplog.text
    assert conn.execute("SELECT COUNT(*) FROM draft_examples").fetchone()[0] == 0


def test_run_draft_records_examples_for_failed_drafts_too(conn, monkeypatch):
    import run_draft
    from draft import drafter

    seed_item(conn, "old", total=9.0, hours_ago=30)
    old = store.insert_draft(
        conn,
        item_id="old",
        model="m",
        draft=store.Draft(f"A game-changer! ORR 88%. {URL}", ["a", "b", f"c {URL}"], "", ""),
    )
    edit_id = store.edit(
        conn,
        old,
        single_post=f"ORR 88% in a single-arm study. Sequencing is open. {URL}",
        thread=["a", "b", f"c {URL}"],
    )
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
    run_draft.main(["--min-score", "7"])
    failed = store.list_drafts(conn, store.STATUS_FAILED)
    assert len(failed) == 1
    assert [r["decision_id"] for r in store.list_examples(conn, failed[0].id)] == [edit_id]
