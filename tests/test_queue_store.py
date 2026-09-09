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


def test_fetch_candidates_uses_latest_score_per_item(conn):
    seed_item(conn, "x", total=2.0, hours_ago=5)
    conn.execute(
        "INSERT INTO scores VALUES (?, 9, 9, 9, 9, 9, 9.5, 'new', 'new angle', ?)",
        ("x", "2999-01-01T00:00:00+00:00"),
    )
    conn.commit()
    cands = store.fetch_candidates(min_score=7.0, since_hours=48, conn=conn)
    assert len(cands) == 1 and cands[0].total == 9.5 and cands[0].suggested_angle == "new angle"


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
