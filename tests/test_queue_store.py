import sqlite3

import pytest

from approval_queue import store
from draft.schema import Claim, Draft
from tests.conftest import URL, seed_item


def make_draft(**kw):
    d = Draft(
        single_post=f"ORR 88%. {URL}",
        thread=["a", "b", f"c {URL}"],
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


def test_fetch_candidates_filters_by_score_and_age(conn):
    seed_item(conn, "hi", total=9.0)
    seed_item(conn, "low", total=3.0)
    seed_item(conn, "old", total=9.5, hours_ago=100)
    seed_item(conn, "pre", source="biorxiv", total=8.0)
    cands = store.fetch_candidates(min_score=7.0, since_hours=48, conn=conn)
    assert [c.item_id for c in cands] == ["hi", "pre"]
    c = cands[0]
    assert c.url == URL and c.suggested_angle == "angle hi" and c.rationale == "rationale hi"
    assert c.scores["novelty"] == 8.0 and c.total == 9.0
    assert "88%" in c.abstract


def test_fetch_candidates_uses_latest_score_per_cluster(conn):
    cid = seed_item(conn, "x", total=2.0, hours_ago=5)
    conn.execute(
        """INSERT INTO scores (cluster_id, model, prompt_version, total, rationale,
                               suggested_angle, raw_response, scored_at)
           VALUES (?, 'm', 'v2', 9.5, 'new', 'new angle', '{}', ?)""",
        (cid, "2999-01-01T00:00:00+00:00"),
    )
    conn.commit()
    cands = store.fetch_candidates(min_score=7.0, since_hours=48, conn=conn)
    assert len(cands) == 1 and cands[0].total == 9.5 and cands[0].suggested_angle == "new angle"
    assert cands[0].cluster_id == cid


def test_fetch_candidates_one_per_cluster_prefers_longest_abstract(conn):
    cid = seed_item(conn, "pr", source="company_x", total=9.0, abstract="short")
    seed_item(conn, "paper", source="pubmed", total=9.0, cluster_id=cid)
    cands = store.fetch_candidates(min_score=7.0, since_hours=48, conn=conn)
    assert [c.item_id for c in cands] == ["paper"]


def test_has_draft_covers_whole_cluster(conn):
    cid = seed_item(conn, "a", total=9.0)
    seed_item(conn, "b", total=9.0, cluster_id=cid)
    store.insert_draft(conn, item_id="a", cluster_id=cid, model="m", draft=make_draft())
    assert store.has_draft(conn, "b", cid)
    assert not store.has_draft(conn, "b")


def test_insert_get_and_one_draft_per_item(conn):
    seed_item(conn, "i1")
    did = store.insert_draft(conn, item_id="i1", model="m", draft=make_draft())
    assert store.has_draft(conn, "i1") and not store.has_draft(conn, "i2")
    row = store.get_draft(conn, did)
    assert row.status == store.STATUS_PENDING
    assert row.draft == make_draft()
    assert row.title == "Title i1" and row.total == 8.5 and row.source == "pubmed"
    with pytest.raises(sqlite3.IntegrityError):
        store.insert_draft(conn, item_id="i1", model="m", draft=make_draft())
    assert store.get_draft(conn, 999) is None


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
    assert decs[0]["original_text"] == make_draft().single_post
    assert decs[0]["edited_text"] is None and decs[0]["note"] == "ok"
    assert store.list_drafts(conn) == []
    assert [r.id for r in store.list_drafts(conn, store.STATUS_APPROVED)] == [did]


def test_edit_saves_original_and_edited_text(conn):
    seed_item(conn, "i1")
    did = store.insert_draft(conn, item_id="i1", model="m", draft=make_draft())
    store.edit(conn, did, single_post=f"Better. {URL}", thread=["x", "y", f"z {URL}"])
    row = store.get_draft(conn, did)
    assert row.status == store.STATUS_APPROVED
    assert row.draft.single_post == f"Better. {URL}" and row.draft.thread == ["x", "y", f"z {URL}"]
    dec = store.list_decisions(conn, did)[0]
    assert dec["action"] == "edit"
    assert "ORR 88%" in dec["original_text"] and '"a"' in dec["original_text"]
    assert "Better." in dec["edited_text"] and '"z ' in dec["edited_text"]


def test_edit_without_approve_stays_pending(conn):
    seed_item(conn, "i1")
    did = store.insert_draft(conn, item_id="i1", model="m", draft=make_draft())
    store.edit(conn, did, single_post="s", thread=["1", "2", "3"], approve_after=False)
    assert store.get_draft(conn, did).status == store.STATUS_PENDING


def test_reject(conn):
    seed_item(conn, "i1")
    did = store.insert_draft(conn, item_id="i1", model="m", draft=make_draft())
    store.reject(conn, did, note="wrong")
    assert store.get_draft(conn, did).status == store.STATUS_REJECTED
    assert store.list_decisions(conn, did)[0]["action"] == "reject"
    assert store.list_drafts(conn) == []


def test_snooze_hides_for_24h_then_reappears(conn):
    seed_item(conn, "i1")
    did = store.insert_draft(conn, item_id="i1", model="m", draft=make_draft())
    store.snooze(conn, did)
    row = store.get_draft(conn, did)
    assert row.status == store.STATUS_SNOOZED and row.snoozed_until is not None
    assert store.list_drafts(conn) == []
    assert store.list_decisions(conn, did)[0]["action"] == "snooze"
    # expire the snooze
    conn.execute(
        "UPDATE drafts SET snoozed_until = '2000-01-01T00:00:00+00:00' WHERE id = ?", (did,)
    )
    conn.commit()
    assert [r.id for r in store.list_drafts(conn)] == [did]


def test_actions_on_missing_draft_raise(conn):
    for fn in (store.approve, store.reject, store.snooze):
        with pytest.raises(KeyError):
            fn(conn, 42)
    with pytest.raises(KeyError):
        store.edit(conn, 42, single_post="s", thread=[])


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
    store.edit(
        conn, did, single_post=f"s {URL}", thread=["1", "2", f"3 {URL}"], category="Factual "
    )
    assert store.list_decisions(conn, did)[0]["category"] == "factual"
    seed_item(conn, "i2")
    did2 = store.insert_draft(conn, item_id="i2", model="m", draft=make_draft())
    store.edit(conn, did2, single_post="s", thread=[], category="")
    assert store.list_decisions(conn, did2)[0]["category"] is None
    with pytest.raises(ValueError):
        store.edit(conn, did2, single_post="s", thread=[], category="nope")


def test_actions_without_category_still_work(conn):
    for i, fn in enumerate((store.approve, store.reject, store.snooze)):
        seed_item(conn, f"i{i}")
        did = store.insert_draft(conn, item_id=f"i{i}", model="m", draft=make_draft())
        fn(conn, did)
        assert store.list_decisions(conn, did)[0]["category"] is None
    assert store.validate_category(None) is None


def test_record_examples_writes_rows_per_kind(conn):
    seed_item(conn, "old")
    seed_item(conn, "new")
    old = store.insert_draft(conn, item_id="old", model="m", draft=make_draft())
    e_id = store.edit(conn, old, single_post=f"edited {URL}", thread=["a", "b", f"c {URL}"])
    r_id = store.reject(conn, old, note="meh")
    new = store.insert_draft(conn, item_id="new", model="m", draft=make_draft())
    assert store.record_examples(conn, new, [e_id], [r_id]) == 2
    rows = store.list_examples(conn, new)
    assert [(r["decision_id"], r["kind"]) for r in rows] == [(e_id, "edit"), (r_id, "rejection")]
    assert all(r["draft_id"] == new and r["created_at"] for r in rows)
    assert store.record_examples(conn, new) == 0
    assert store.list_examples(conn, old) == []


def test_fetch_decisions_for_voice_joins_items_source_and_url(conn):
    seed_item(conn, "i1", source="biorxiv")
    did = store.insert_draft(conn, item_id="i1", model="m", draft=make_draft())
    store.edit(conn, did, single_post=f"e {URL}", thread=["1", "2", f"3 {URL}"], category="voice")
    store.reject(conn, did, note="later")
    rows = store.fetch_decisions_for_voice(conn, "2000-01-01T00:00:00+00:00")
    assert [r["action"] for r in rows] == ["edit", "reject"]  # oldest first
    r = rows[0]
    assert r["source"] == "biorxiv" and r["url"] == URL and r["draft_id"] == did
    assert r["category"] == "voice" and r["draft_status"] == "rejected"
    assert '"single_post"' in r["original_text"] and "e " in r["edited_text"]
    assert r["draft_created_at"] and r["item_id"] == "i1"
    # since in the future -> nothing; datetime accepted too
    from datetime import UTC, datetime, timedelta

    assert store.fetch_decisions_for_voice(conn, datetime.now(UTC) + timedelta(days=1)) == []
    assert len(store.fetch_decisions_for_voice(conn, datetime.now(UTC) - timedelta(days=1))) == 2
    assert len(store.fetch_decisions_for_voice(conn)) == 2


def test_fetch_decisions_for_voice_without_step1_tables(tmp_path):
    conn = store.connect(tmp_path / "solo.db")
    did = store.insert_draft(conn, item_id="orphan", model="m", draft=make_draft())
    store.approve(conn, did, note="fine")
    rows = store.fetch_decisions_for_voice(conn, "2000-01-01")
    assert len(rows) == 1 and rows[0]["source"] == "" and rows[0]["url"] == ""
    stats = store.fetch_draft_stats(conn, "2000-01-01")
    assert len(stats) == 1 and stats[0]["source"] == "" and stats[0]["status"] == "approved"
    conn.close()


def test_fetch_draft_stats(conn):
    seed_item(conn, "a", source="pubmed")
    seed_item(conn, "b", source="fda")
    d1 = store.insert_draft(conn, item_id="a", model="m", draft=make_draft())
    d2 = store.insert_draft(
        conn, item_id="b", model="m", draft=make_draft(), status=store.STATUS_FAILED
    )
    store.reject(conn, d1)
    rows = store.fetch_draft_stats(conn, "2000-01-01")
    assert [(r["id"], r["source"], r["status"]) for r in rows] == [
        (d1, "pubmed", "rejected"),
        (d2, "fda", "failed"),
    ]
    assert rows[0]["item_id"] == "a" and rows[0]["created_at"] and rows[0]["model"] == "m"
