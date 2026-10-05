import json

from approval_queue import store
from tests.conftest import URL, seed_item

# Only numbers that tests.conftest.ABSTRACT contains.
CHART = {"title": "Outcomes", "labels": ["ORR", "PFS"], "values": [88, 14.6], "unit": ""}


def good_json(last="ORR 88% in 97 patients."):
    """A valid model output; `last` is the final thread post."""
    return json.dumps(
        {
            "thread": ["a", "b", last],
            "suggested_visual": "v",
            "why_it_matters": "w",
            "claims_to_verify": [],
            "chart": CHART,
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
        draft=store.Draft(["x", "b", "c"], "", ""),
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
            call=lambda s, u, m: good_json(f"a link sneaks in {URL}"),
            sleep=lambda s: None,
            max_attempts=2,
            **kw,
        ),
    )
    run_draft.main(["--min-score", "7"])
    failed = store.list_drafts(conn, store.STATUS_FAILED)
    assert len(failed) == 1 and "contains a link" in failed[0].rejection_reason
    assert failed[0].draft.thread == [] and failed[0].draft.visual is None
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
    """Run run_draft.main with a fake model call; return the system prompts it saw."""
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
        draft=store.Draft(["A game-changer! ORR 88%.", "b", "c"], "", ""),
    )
    before = "A game-changer! ORR 88%."
    after = "ORR 88% in a single-arm study. The sequencing question is open."
    edit_id = store.edit(
        conn,
        old,
        thread=[after, "b", "c"],
        note="less hype",
        category="voice",
    )
    reject_target = store.insert_draft(
        conn,
        item_id="rej",
        model="m",
        draft=store.Draft(["Meh", "b", "c"], "", ""),
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
        draft=store.Draft(["A game-changer! ORR 88%.", "b", "c"], "", ""),
    )
    store.edit(
        conn,
        old,
        thread=["ORR 88% in a single-arm study. Sequencing is open.", "b", "c"],
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
        draft=store.Draft(["A game-changer! ORR 88%.", "b", "c"], "", ""),
    )
    edit_id = store.edit(
        conn,
        old,
        thread=["ORR 88% in a single-arm study. Sequencing is open.", "b", "c"],
    )
    seed_item(conn, "bad", total=9.0)
    monkeypatch.setattr(
        run_draft,
        "draft_item",
        lambda **kw: drafter.draft_item(
            call=lambda s, u, m: good_json(f"a link sneaks in {URL}"),
            sleep=lambda s: None,
            max_attempts=2,
            **kw,
        ),
    )
    run_draft.main(["--min-score", "7"])
    failed = store.list_drafts(conn, store.STATUS_FAILED)
    assert len(failed) == 1
    assert [r["decision_id"] for r in store.list_examples(conn, failed[0].id)] == [edit_id]


def test_run_draft_retry_failed_redrafts_only_with_the_flag(conn, monkeypatch):
    import run_draft
    from draft import drafter

    seed_item(conn, "bad", total=9.0)
    replies = {"text": f"a link sneaks in {URL}"}
    monkeypatch.setattr(
        run_draft,
        "draft_item",
        lambda **kw: drafter.draft_item(
            call=lambda s, u, m: good_json(replies["text"]),
            sleep=lambda s: None,
            max_attempts=1,
            **kw,
        ),
    )
    run_draft.main(["--min-score", "7"])
    assert len(store.list_drafts(conn, store.STATUS_FAILED)) == 1

    # a plain rerun leaves the failed draft alone
    run_draft.main(["--min-score", "7"])
    assert len(store.list_drafts(conn, store.STATUS_FAILED)) == 1
    assert store.list_drafts(conn) == []

    # --retry-failed replaces it with a good draft
    replies["text"] = "fixed"
    run_draft.main(["--min-score", "7", "--retry-failed"])
    assert store.list_drafts(conn, store.STATUS_FAILED) == []
    assert len(store.list_drafts(conn)) == 1


def test_run_draft_leaves_the_studios_stories_to_the_studio(conn, monkeypatch, caplog):
    """One story, one piece of writing. A studio piece that was not discarded holds its
    story at any stage (waiting at the research checkpoint, still running, failed or
    interrupted: each lands in the queue later), and so does a topic queued on the studio
    page that no piece took yet; the drafter skips them. A story the studio gave up on is
    drafted as before, and a story linking merged is followed through the studio's item."""
    import logging

    import run_draft
    from db import Database
    from studio import store as studio_store

    studio_store.ensure_tables(conn)
    names = ("waiting", "running", "failed", "interrupted", "queued", "discarded", "claimed")
    stories = {name: seed_item(conn, name, total=9.0) for name in (*names, "free")}

    def piece(cluster_id, stage, story_item=""):
        pid = studio_store.create_piece(
            conn,
            origin="manual",
            topic="",
            cluster_id=cluster_id,
            requested_angle="",
            checkpoint=True,
            session_id=f"session-{cluster_id}",
            workspace="/data/studio_pieces/p",
            model="writer-model",
            effort="max",
            story_item=story_item,
        )
        studio_store.update_piece(conn, pid, stage=stage)
        return pid

    piece(stories["waiting"], studio_store.STAGE_RESEARCH_READY)
    piece(stories["running"], studio_store.STAGE_WRITING)
    piece(stories["failed"], studio_store.STAGE_FAILED)
    piece(stories["interrupted"], studio_store.STAGE_INTERRUPTED)
    piece(stories["discarded"], studio_store.STAGE_DISCARDED)
    studio_store.queue_topic(conn, cluster_id=stories["queued"])
    taken = studio_store.queue_topic(conn, cluster_id=stories["claimed"])
    studio_store.claim_topic(conn, taken, piece(stories["claimed"], studio_store.STAGE_DISCARDED))
    # linking folds the studio's story into another cluster before the studio runs again
    kept = seed_item(conn, "kept", total=9.0)
    folded = seed_item(conn, "folded", total=9.0)
    piece(folded, studio_store.STAGE_RESEARCH_READY, story_item="folded")
    database = Database(str(store.db_path()))
    database.merge_clusters(kept, [folded])
    database.close()

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
    with caplog.at_level(logging.INFO, logger="run_draft"):
        assert run_draft.main(["--min-score", "7"]) == 0
    assert {r.item_id for r in store.list_drafts(conn)} == {"discarded", "claimed", "free"}
    assert len(calls) == 3
    assert "6 left to the studio (a studio piece or queued topic has the story)" in caplog.text


def test_run_draft_drafts_as_before_without_the_studios_tables(conn, monkeypatch):
    import run_draft

    assert store.studio_held_clusters(conn) == set()
    seed_item(conn, "new", total=9.0)
    monkeypatch.setattr(
        run_draft,
        "draft_item",
        lambda **kw: __import__("draft.drafter").drafter.draft_item(
            call=lambda s, u, m: good_json(), sleep=lambda s: None, **kw
        ),
    )
    assert run_draft.main(["--min-score", "7"]) == 0
    assert [r.item_id for r in store.list_drafts(conn)] == ["new"]


def _studio_piece(conn, *, stage, cluster_id=None, offered=()):
    """A studio piece as studio/session.py:research leaves it: the stories it was offered in
    its meta, its own story once research named one."""
    from studio import store as studio_store

    studio_store.ensure_tables(conn)
    pid = studio_store.create_piece(
        conn,
        origin="auto",
        topic="",
        cluster_id=None,
        requested_angle="",
        checkpoint=False,
        session_id=f"session-{stage}-{len(offered)}",
        workspace="/data/studio_pieces/p",
        model="writer-model",
        effort="max",
    )
    studio_store.update_piece(
        conn, pid, stage=stage, cluster_id=cluster_id, meta={"offered_stories": list(offered)}
    )
    return pid


def _draft_by_hand(monkeypatch, on_call):
    import run_draft

    monkeypatch.setattr(
        run_draft,
        "draft_item",
        lambda **kw: __import__("draft.drafter").drafter.draft_item(
            call=on_call, sleep=lambda s: None, **kw
        ),
    )


def test_run_draft_leaves_the_stories_a_researching_piece_was_offered(conn, monkeypatch, caplog):
    """A studio piece researching on no story yet may name any story it was offered, and
    those are the undrafted top stories: the drafter leaves them alone until research names
    one (or stops), then drafts the rest."""
    import logging

    import run_draft
    from studio import store as studio_store

    offered = [seed_item(conn, name, total=9.0) for name in ("offer-a", "offer-b")]
    stopped = seed_item(conn, "offered-to-a-stopped-research", total=9.0)
    seed_item(conn, "free", total=9.0)
    pid = _studio_piece(conn, stage=studio_store.STAGE_RESEARCHING, offered=offered)
    _studio_piece(conn, stage=studio_store.STAGE_FAILED, offered=[stopped])
    _draft_by_hand(monkeypatch, lambda s, u, m: good_json())

    with caplog.at_level(logging.INFO, logger="run_draft"):
        assert run_draft.main(["--min-score", "7"]) == 0
    drafted = {r.item_id for r in store.list_drafts(conn)}
    assert drafted == {"offered-to-a-stopped-research", "free"}
    assert "2 left to the studio" in caplog.text

    # research named offer-a: offer-b is the drafter's again
    studio_store.update_piece(conn, pid, cluster_id=offered[0])
    assert run_draft.main(["--min-score", "7"]) == 0
    assert {r.item_id for r in store.list_drafts(conn)} == drafted | {"offer-b"}


def test_run_draft_looks_at_the_studios_hold_again_before_each_story(conn, monkeypatch, caplog):
    """A draft step works through its list for an hour; a studio session that starts
    researching beside it (cron's studio entry, Start now on the panel) is offered the
    stories the step has not reached yet, and the step leaves them to it."""
    import logging

    import run_draft
    from studio import store as studio_store

    first = seed_item(conn, "first", total=9.5)
    later = seed_item(conn, "later", total=9.0)
    calls = []

    def call(system, user, model):
        if not calls:  # the studio starts while the first story is being drafted
            _studio_piece(conn, stage=studio_store.STAGE_RESEARCHING, offered=[first, later])
        calls.append(user)
        return good_json()

    _draft_by_hand(monkeypatch, call)
    with caplog.at_level(logging.INFO, logger="run_draft"):
        assert run_draft.main(["--min-score", "7"]) == 0

    # the story already in hand is stored (a research that names it is refused instead)
    assert [r.item_id for r in store.list_drafts(conn)] == ["first"]
    assert len(calls) == 1
    assert "left to the studio since this run started: Title later" in caplog.text


def test_a_draft_whose_story_a_studio_piece_took_meanwhile_is_not_stored(conn, monkeypatch, caplog):
    """A studio research that names the story the drafter is still writing wins it: the
    piece goes on to write it, so the drafter's thread is not stored, and the next run
    leaves the story alone too."""
    import logging

    import run_draft
    from studio import store as studio_store

    story = seed_item(conn, "taken", total=9.0)

    def call(system, user, model):
        _studio_piece(conn, stage=studio_store.STAGE_WRITING, cluster_id=story, offered=[story])
        return good_json()

    _draft_by_hand(monkeypatch, call)
    with caplog.at_level(logging.WARNING, logger="run_draft"):
        assert run_draft.main(["--min-score", "7"]) == 0

    assert conn.execute("SELECT COUNT(*) FROM drafts").fetchone()[0] == 0
    assert "taken: the studio took this story while it was drafted" in caplog.text


# --- the CLI failing is not the model failing --------------------------------------------


def _draft_with(monkeypatch, call):
    import run_draft
    from draft import drafter

    monkeypatch.setattr(
        run_draft,
        "draft_item",
        lambda **kw: drafter.draft_item(call=call, sleep=lambda s: None, **kw),
    )
    return run_draft


def test_a_story_the_safeguard_refuses_is_stored_failed_and_not_sent_again(conn, monkeypatch):
    """The same prompt is refused every time: four refused calls a story, every run, for
    ever for a story the editor said yes to. Now one call, and the story is stored failed
    with the reason (--retry-failed sends it once more)."""
    import claude_cli

    calls = []

    def refuse(system, user, model):
        calls.append(user)
        raise claude_cli.ClaudeCliRefused("CLI exited 1: safeguards flagged this message")

    run_draft = _draft_with(monkeypatch, refuse)
    seed_item(conn, "flagged", total=9.0)
    assert run_draft.main(["--min-score", "7"]) == 0
    [failed] = store.list_drafts(conn, store.STATUS_FAILED)
    assert failed.rejection_reason.startswith(f"{run_draft.REFUSED}: CLI exited 1: safeguards")
    assert failed.model == "ClaudeCliRefused" and len(calls) == 1
    run_draft.main(["--min-score", "7"])
    assert len(calls) == 1  # not sent again


def test_a_cli_that_cannot_start_ends_the_run(conn, monkeypatch):
    import claude_cli

    calls = []

    def missing(system, user, model):
        calls.append(user)
        raise claude_cli.ClaudeCliUnavailable("'claude' not found on PATH")

    run_draft = _draft_with(monkeypatch, missing)
    for name in ("one", "two", "three"):
        seed_item(conn, name, total=9.0)
    assert run_draft.main(["--min-score", "7"]) == 0
    assert len(calls) == 1  # one try, not three stories times four attempts with backoff
    assert store.list_drafts(conn, store.STATUS_FAILED) == [] and store.list_drafts(conn) == []


def test_a_usage_limit_after_a_rule_failure_leaves_the_story_for_the_next_run(conn, monkeypatch):
    """Attempt 1 broke a hard rule and the usage limit hit the rest: nothing is stored, so
    the next run drafts the story instead of it sitting failed with the old reason."""
    import claude_cli

    replies = iter([good_json(f"a link sneaks in {URL}")])

    def limited(system, user, model):
        reply = next(replies, None)
        if reply is None:
            raise claude_cli.ClaudeCliError("CLI exited 1: usage limit reached")
        return reply

    run_draft = _draft_with(monkeypatch, limited)
    seed_item(conn, "later", total=9.0)
    assert run_draft.main(["--min-score", "7"]) == 0
    assert store.list_drafts(conn, store.STATUS_FAILED) == [] and store.list_drafts(conn) == []
    _draft_with(monkeypatch, lambda s, u, m: good_json())
    run_draft.main(["--min-score", "7"])
    [row] = store.list_drafts(conn)
    assert row.item_id == "later" and row.status == store.STATUS_PENDING
