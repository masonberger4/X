import sqlite3

import pytest

from approval_queue import store
from draft.schema import Claim, Draft
from tests.conftest import seed_item


def make_draft(**kw):
    d = Draft(
        thread=["ORR 88%.", "a", "b", "c"],
        suggested_visual="plot",
        why_it_matters="because",
        claims_to_verify=[Claim("ORR 88%", "high")],
    )
    for k, v in kw.items():
        setattr(d, k, v)
    return d


def test_connect_creates_own_tables_and_leaves_step1_alone(db_file):
    conn = store.connect(db_file)
    names = {
        r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    }
    assert {"drafts", "decisions", "items", "scores"} <= names
    # idempotent
    store.connect(db_file).close()
    conn.close()


def test_connect_uses_db_path_env(tmp_path, monkeypatch):
    p = tmp_path / "other.db"
    monkeypatch.setenv("DB_PATH", str(p))
    store.connect().close()
    assert p.exists()


def test_drafts_created_at_index_created_idempotently(db_file):
    c1 = store.connect(db_file)
    c1.close()
    c2 = store.connect(db_file)
    try:
        rows = c2.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name='idx_drafts_created'"
        ).fetchall()
        assert len(rows) == 1
    finally:
        c2.close()


def test_drafted_cluster_ids_are_every_story_with_a_draft_that_did_not_fail(conn):
    from db import Database

    ids = {}
    for status in store.STATUSES:
        ids[status] = seed_item(conn, status, total=9.0)
        store.insert_draft(
            conn,
            item_id=status,
            cluster_id=ids[status],
            model="m",
            draft=make_draft(),
            status=status,
        )
    unclustered = seed_item(conn, "no-cluster-on-the-draft", total=9.0)
    store.insert_draft(conn, item_id="no-cluster-on-the-draft", model="m", draft=make_draft())
    # linking folds a drafted story into another cluster: the draft still counts there
    kept = seed_item(conn, "kept", total=9.0)
    database = Database(str(store.db_path()))
    database.merge_clusters(kept, [ids[store.STATUS_APPROVED]])
    database.close()

    expected = {cid for status, cid in ids.items() if status != store.STATUS_FAILED}
    assert store.drafted_cluster_ids(conn) == expected | {unclustered, kept}


def test_failed_draft_stored_with_reason(conn):
    seed_item(conn, "i1")
    did = store.insert_draft(
        conn,
        item_id="i1",
        model="m",
        draft=make_draft(),
        status=store.STATUS_FAILED,
        rejection_reason="too long",
    )
    row = store.get_draft(conn, did)
    assert row.status == "failed" and row.rejection_reason == "too long"
    assert store.list_drafts(conn) == []  # not pending


def test_drafts_work_without_step1_tables(tmp_path):
    conn = store.connect(tmp_path / "solo.db")
    did = store.insert_draft(conn, item_id="orphan", model="m", draft=make_draft())
    row = store.get_draft(conn, did)
    assert row.title == "" and row.total is None
    assert [r.id for r in store.list_drafts(conn)] == [did]


def test_approve_records_decision_and_does_not_publish(conn):
    seed_item(conn, "i1")
    did = store.insert_draft(conn, item_id="i1", model="m", draft=make_draft())
    store.approve(conn, did, note="ok")
    row = store.get_draft(conn, did)
    assert row.status == store.STATUS_APPROVED
    decs = store.list_decisions(conn, did)
    assert len(decs) == 1
    assert decs[0]["action"] == "approve"
    assert decs[0]["original_text"] == store._serialise_text(make_draft().thread)
    assert decs[0]["edited_text"] is None and decs[0]["note"] == "ok"
    assert store.list_drafts(conn) == []
    assert [r.id for r in store.list_drafts(conn, store.STATUS_APPROVED)] == [did]


def test_edit_saves_original_and_edited_text(conn):
    seed_item(conn, "i1")
    did = store.insert_draft(conn, item_id="i1", model="m", draft=make_draft())
    store.edit(conn, did, thread=["Better.", "x", "y", "z"])
    row = store.get_draft(conn, did)
    assert row.status == store.STATUS_APPROVED
    assert row.draft.thread == ["Better.", "x", "y", "z"]
    dec = store.list_decisions(conn, did)[0]
    assert dec["action"] == "edit"
    assert "ORR 88%" in dec["original_text"] and '"a"' in dec["original_text"]
    assert "Better." in dec["edited_text"] and '"z"' in dec["edited_text"]


def test_edit_without_approve_stays_pending(conn):
    seed_item(conn, "i1")
    did = store.insert_draft(conn, item_id="i1", model="m", draft=make_draft())
    store.edit(conn, did, thread=["1", "2", "3"], approve_after=False)
    assert store.get_draft(conn, did).status == store.STATUS_PENDING


def test_reject(conn):
    seed_item(conn, "i1")
    did = store.insert_draft(conn, item_id="i1", model="m", draft=make_draft())
    store.reject(conn, did, note="wrong")
    assert store.get_draft(conn, did).status == store.STATUS_REJECTED
    assert store.list_decisions(conn, did)[0]["action"] == "reject"
    assert store.list_drafts(conn) == []


def test_reopen_moves_an_approved_draft_back_to_pending(conn):
    seed_item(conn, "i1")
    did = store.insert_draft(conn, item_id="i1", model="m", draft=make_draft())
    store.approve(conn, did, note="ok")
    assert store.list_drafts(conn) == []
    store.reopen(conn, did, note="second thoughts")
    row = store.get_draft(conn, did)
    assert row.status == store.STATUS_PENDING
    assert [r.id for r in store.list_drafts(conn)] == [did]
    dec = store.list_decisions(conn, did)[-1]
    assert dec["action"] == "reopen"
    assert dec["original_text"] == store._serialise_text(make_draft().thread)
    # edited_text stays NULL: a reopen is not an edit and must never be taught as one
    assert dec["edited_text"] is None
    assert dec["note"] == "second thoughts" and dec["category"] is None


def test_actions_on_missing_draft_raise(conn):
    for fn in (store.approve, store.reject, store.reopen):
        with pytest.raises(KeyError):
            fn(conn, 42)
    with pytest.raises(KeyError):
        store.edit(conn, 42, thread=[])


def test_list_pending_newest_first(conn):
    seed_item(conn, "a")
    seed_item(conn, "b")
    d1 = store.insert_draft(conn, item_id="a", model="m", draft=make_draft())
    d2 = store.insert_draft(conn, item_id="b", model="m", draft=make_draft())
    assert [r.id for r in store.list_drafts(conn)] == [d2, d1]


# --- step 7: category migration, draft_examples, voice adapters ------------------


OLD_DECISIONS_SCHEMA = """
CREATE TABLE drafts (
    id INTEGER PRIMARY KEY AUTOINCREMENT, item_id TEXT NOT NULL UNIQUE, cluster_id INTEGER,
    model TEXT NOT NULL, single_post TEXT NOT NULL, thread_json TEXT NOT NULL,
    suggested_visual TEXT NOT NULL DEFAULT '', why_it_matters TEXT NOT NULL DEFAULT '',
    claims_json TEXT NOT NULL DEFAULT '[]', status TEXT NOT NULL DEFAULT 'pending',
    rejection_reason TEXT, snoozed_until TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT, draft_id INTEGER NOT NULL REFERENCES drafts(id),
    action TEXT NOT NULL, original_text TEXT NOT NULL, edited_text TEXT, note TEXT,
    created_at TEXT NOT NULL
);
"""


def _columns(conn, table):
    return [r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]


def test_connect_migrates_old_decisions_schema_and_keeps_rows(tmp_path):
    path = tmp_path / "old.db"
    raw = sqlite3.connect(path)
    raw.executescript(OLD_DECISIONS_SCHEMA)
    raw.execute(
        "INSERT INTO drafts (item_id, model, single_post, thread_json, created_at, updated_at)"
        " VALUES ('i', 'm', 's', '[]', '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00')"
    )
    raw.execute(
        "INSERT INTO decisions (draft_id, action, original_text, created_at)"
        " VALUES (1, 'approve', 's', '2026-01-01T00:00:00+00:00')"
    )
    raw.commit()
    assert "category" not in _columns(raw, "decisions")
    raw.close()

    conn = store.connect(path)
    assert "category" in _columns(conn, "decisions")
    assert "single_post" not in _columns(conn, "drafts")  # threads only
    assert "draft_examples" in {
        r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    old = store.list_decisions(conn)
    assert len(old) == 1 and old[0]["category"] is None and old[0]["action"] == "approve"
    # old-style callers (steps 4 and 5 select named columns) still work
    assert conn.execute("SELECT edited_text, original_text FROM decisions").fetchone()[1] == "s"
    conn.close()
    # idempotent: a second connect neither fails nor duplicates the column
    conn = store.connect(path)
    assert _columns(conn, "decisions").count("category") == 1
    conn.close()


def test_connect_drops_a_not_null_single_post_column_and_inserts_still_work(tmp_path):
    path = tmp_path / "old.db"
    raw = sqlite3.connect(path)
    raw.executescript(OLD_DECISIONS_SCHEMA)
    raw.execute(
        "INSERT INTO drafts (item_id, model, single_post, thread_json, created_at, updated_at)"
        " VALUES ('i', 'm', 's', '[\"a\"]', '2026-01-01T00:00:00+00:00',"
        " '2026-01-01T00:00:00+00:00')"
    )
    raw.commit()
    assert "single_post" in _columns(raw, "drafts")
    raw.close()

    conn = store.connect(path)
    assert "single_post" not in _columns(conn, "drafts")
    assert store.get_draft(conn, 1).draft.thread == ["a"]  # the old row survives
    did = store.insert_draft(conn, item_id="new", model="m", draft=make_draft())
    assert store.get_draft(conn, did).draft == make_draft()
    conn.close()
    store.connect(path).close()  # idempotent


def test_connect_turns_a_snoozed_draft_back_into_a_pending_one(tmp_path):
    path = tmp_path / "snoozed.db"
    conn = store.connect(path)
    did = store.insert_draft(conn, item_id="i1", model="m", draft=make_draft())
    conn.execute("UPDATE drafts SET status = 'snoozed' WHERE id = ?", (did,))
    conn.commit()
    conn.close()

    conn = store.connect(path)  # _migrate runs again on this handle
    assert store.get_draft(conn, did).status == store.STATUS_PENDING
    assert [r.id for r in store.list_drafts(conn)] == [did]
    conn.close()


def test_the_snooze_migration_moves_nothing_else(tmp_path):
    """It runs on every connect, so a WHERE clause lost here would quietly un-approve and
    un-reject every draft in a live database."""
    path = tmp_path / "mixed.db"
    conn = store.connect(path)
    ids = {}
    for name in ("snoozed", "approved", "rejected", "failed", "pending"):
        did = store.insert_draft(conn, item_id=f"i_{name}", model="m", draft=make_draft())
        conn.execute("UPDATE drafts SET status = ? WHERE id = ?", (name, did))
        ids[name] = did
    conn.commit()
    conn.close()

    conn = store.connect(path)
    after = {name: store.get_draft(conn, did).status for name, did in ids.items()}
    assert after == {
        "snoozed": store.STATUS_PENDING,  # the only one that moves
        "approved": store.STATUS_APPROVED,
        "rejected": store.STATUS_REJECTED,
        "failed": store.STATUS_FAILED,
        "pending": store.STATUS_PENDING,
    }
    conn.close()


def test_connect_is_idempotent_on_new_database(tmp_path):
    path = tmp_path / "new.db"
    store.connect(path).close()
    conn = store.connect(path)
    assert _columns(conn, "decisions").count("category") == 1
    assert _columns(conn, "draft_examples") == [
        "id",
        "draft_id",
        "decision_id",
        "kind",
        "created_at",
    ]
    conn.close()


def test_reject_with_category_stores_it_and_invalid_raises(conn):
    seed_item(conn, "i1")
    did = store.insert_draft(conn, item_id="i1", model="m", draft=make_draft())
    with pytest.raises(ValueError, match="unknown category"):
        store.reject(conn, did, note="x", category="bogus")
    assert store.get_draft(conn, did).status == store.STATUS_PENDING  # nothing changed
    store.reject(conn, did, note="too hypey", category="voice")
    dec = store.list_decisions(conn, did)[0]
    assert dec["category"] == "voice" and dec["note"] == "too hypey" and dec["action"] == "reject"


def test_edit_with_category_and_blank_category_is_none(conn):
    seed_item(conn, "i1")
    did = store.insert_draft(conn, item_id="i1", model="m", draft=make_draft())
    store.edit(conn, did, thread=["1", "2", "3"], category="Factual ")
    assert store.list_decisions(conn, did)[0]["category"] == "factual"
    seed_item(conn, "i2")
    did2 = store.insert_draft(conn, item_id="i2", model="m", draft=make_draft())
    store.edit(conn, did2, thread=[], category="")
    assert store.list_decisions(conn, did2)[0]["category"] is None
    with pytest.raises(ValueError):
        store.edit(conn, did2, thread=[], category="nope")


def test_actions_without_category_still_work(conn):
    for i, fn in enumerate((store.approve, store.reject, store.reopen)):
        seed_item(conn, f"i{i}")
        did = store.insert_draft(conn, item_id=f"i{i}", model="m", draft=make_draft())
        fn(conn, did)
        assert store.list_decisions(conn, did)[0]["category"] is None
    assert store.validate_category(None) is None


def test_revise_replaces_whole_draft_keeps_status_and_logs_decision(conn):
    seed_item(conn, "i1")
    d = Draft(
        thread=["old", "a", "b", "c"],
        suggested_visual="v",
        why_it_matters="w",
        claims_to_verify=[Claim("old claim", "low")],
    )
    did = store.insert_draft(conn, item_id="i1", model="m1", draft=d)
    new = Draft(
        thread=["new", "x", "y", "z"],
        suggested_visual="v2",
        why_it_matters="w2",
        claims_to_verify=[Claim("new claim", "medium")],
    )
    store.revise(conn, did, draft=new, model="m2", note="less hype", category="voice")
    row = store.get_draft(conn, did)
    assert row.status == "pending"
    assert row.model == "m2"
    assert row.draft.thread == ["new", "x", "y", "z"]
    assert row.draft.why_it_matters == "w2" and row.draft.suggested_visual == "v2"
    assert [c.claim for c in row.draft.claims_to_verify] == ["new claim"]
    (dec,) = store.list_decisions(conn, did)
    assert dec["action"] == "revise"
    assert dec["note"] == "less hype" and dec["category"] == "voice"
    assert "old" in dec["original_text"] and "new" in dec["edited_text"]
    with pytest.raises(KeyError):
        store.revise(conn, 999, draft=new, model="m")
