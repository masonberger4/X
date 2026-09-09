import pytest
from fastapi.testclient import TestClient

from approval_queue import store
from approval_queue.app import app
from draft.schema import Claim, Draft
from tests.conftest import URL, seed_item


@pytest.fixture
def client(db_file):
    # db_file sets DB_PATH so the app's per-request connections hit the temp file
    return TestClient(app, follow_redirects=False)


@pytest.fixture
def draft_id(conn):
    seed_item(conn, "i1", source="biorxiv")
    d = Draft(
        single_post=f"Preprint: ORR 88%. {URL}",
        thread=["Preprint. one", "two", f"three {URL}"],
        suggested_visual="plot",
        why_it_matters="matters",
        claims_to_verify=[Claim("Number '15' does not appear in the source abstract", "low")],
    )
    return store.insert_draft(conn, item_id="i1", model="m", draft=d)


def test_index_lists_pending_with_source_score_rationale(client, draft_id):
    r = client.get("/")
    assert r.status_code == 200
    body = r.text
    assert "biorxiv" in body and "8.5" in body and "rationale i1" in body
    assert f"/drafts/{draft_id}" in body


def test_detail_shows_post_thread_claims_and_lengths(client, draft_id):
    r = client.get(f"/drafts/{draft_id}")
    assert r.status_code == 200
    body = r.text
    assert "Preprint: ORR 88%." in body
    assert "1/3 Preprint. one" in body and "3/3 three" in body
    assert "[low]" in body and "does not appear" in body
    assert "/280" in body
    assert client.get("/drafts/999").status_code == 404


def test_approve_action(client, conn, draft_id):
    r = client.post(f"/drafts/{draft_id}/approve", data={"note": "good"})
    assert r.status_code == 303 and r.headers["location"] == "/"
    row = store.get_draft(conn, draft_id)
    assert row.status == "approved"
    dec = store.list_decisions(conn, draft_id)
    assert dec[0]["action"] == "approve" and dec[0]["note"] == "good"
    assert f"/drafts/{draft_id}" not in client.get("/").text
    assert f"/drafts/{draft_id}" in client.get("/status/approved").text


def test_edit_action_saves_original_and_edited(client, conn, draft_id):
    r = client.post(
        f"/drafts/{draft_id}/edit",
        data={
            "single_post": f"Preprint, edited. {URL}",
            "thread": f"Preprint. first\n---\nsecond\n---\nlast {URL}",
            "note": "tightened",
        },
    )
    assert r.status_code == 303
    row = store.get_draft(conn, draft_id)
    assert row.status == "approved"
    assert row.draft.single_post == f"Preprint, edited. {URL}"
    assert row.draft.thread == ["Preprint. first", "second", f"last {URL}"]
    dec = store.list_decisions(conn, draft_id)[0]
    assert dec["action"] == "edit"
    assert "ORR 88%" in dec["original_text"]
    assert "edited" in dec["edited_text"]
    assert dec["note"] == "tightened"


def test_edit_keep_pending(client, conn, draft_id):
    r = client.post(
        f"/drafts/{draft_id}/edit",
        data={"single_post": "s", "thread": "a\n---\nb\n---\nc", "keep_pending": "1"},
    )
    assert r.status_code == 303 and r.headers["location"] == f"/drafts/{draft_id}"
    assert store.get_draft(conn, draft_id).status == "pending"


def test_edit_rejects_over_280_and_empty(client, conn, draft_id):
    r = client.post(f"/drafts/{draft_id}/edit", data={"single_post": "x" * 281, "thread": ""})
    assert r.status_code == 400 and "280" in r.text
    r = client.post(f"/drafts/{draft_id}/edit", data={"single_post": "", "thread": "a"})
    assert r.status_code == 400
    assert store.get_draft(conn, draft_id).status == "pending"
    assert store.list_decisions(conn, draft_id) == []


def test_reject_action(client, conn, draft_id):
    r = client.post(f"/drafts/{draft_id}/reject", data={"note": "wrong take"})
    assert r.status_code == 303
    assert store.get_draft(conn, draft_id).status == "rejected"
    assert store.list_decisions(conn, draft_id)[0]["note"] == "wrong take"
    assert f"/drafts/{draft_id}" in client.get("/status/rejected").text


def test_snooze_action_hides_for_24h(client, conn, draft_id):
    r = client.post(f"/drafts/{draft_id}/snooze")
    assert r.status_code == 303
    row = store.get_draft(conn, draft_id)
    assert row.status == "snoozed" and row.snoozed_until is not None
    assert f"/drafts/{draft_id}" not in client.get("/").text
    assert f"/drafts/{draft_id}" in client.get("/status/snoozed").text


def test_actions_on_missing_draft_404(client):
    for action in ("approve", "reject", "snooze"):
        assert client.post(f"/drafts/999/{action}").status_code == 404
    assert (
        client.post("/drafts/999/edit", data={"single_post": "s", "thread": ""}).status_code == 404
    )
    assert client.get("/status/bogus").status_code == 404
