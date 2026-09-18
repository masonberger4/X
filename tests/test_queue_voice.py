"""Step 7 queue tests: categories in the UI, the /voice page and the decision diff."""

import pytest
from fastapi.testclient import TestClient

from approval_queue import store
from approval_queue.app import app, decision_diff
from draft.schema import Draft
from tests.conftest import seed_item


@pytest.fixture
def client(db_file):
    return TestClient(app, follow_redirects=False)


def _draft(conn, item_id="i1", source="pubmed"):
    seed_item(conn, item_id, source=source)
    d = Draft(
        thread=[
            "A game-changer: ORR 88% in 97 patients. Exciting.",
            "Single-arm.",
            "",
        ],
        suggested_visual="plot",
        why_it_matters="matters",
    )
    return store.insert_draft(conn, item_id=item_id, model="m", draft=d)


def test_forms_offer_categories(client, conn):
    did = _draft(conn)
    body = client.get(f"/drafts/{did}").text
    assert body.count('name="category"') == 3  # reject, revise and edit forms
    for c in store.DECISION_CATEGORIES:
        assert f'<option value="{c}">' in body


def test_reject_form_with_category_persists(client, conn):
    did = _draft(conn)
    r = client.post(f"/drafts/{did}/reject", data={"note": "hypey", "category": "voice"})
    assert r.status_code == 303
    dec = store.list_decisions(conn, did)[0]
    assert dec["category"] == "voice" and dec["note"] == "hypey"
    assert store.get_draft(conn, did).status == "rejected"


def test_reject_form_without_category_and_with_invalid_category(client, conn):
    did = _draft(conn)
    assert (
        client.post(f"/drafts/{did}/reject", data={"note": "", "category": "nope"}).status_code
        == 400
    )
    assert store.get_draft(conn, did).status == "pending"
    assert client.post(f"/drafts/{did}/reject", data={"category": ""}).status_code == 303
    assert store.list_decisions(conn, did)[0]["category"] is None


def test_edit_form_with_category_persists_and_detail_shows_diff(client, conn):
    did = _draft(conn)
    r = client.post(
        f"/drafts/{did}/edit",
        data={
            "thread": (
                "ORR 88% in 97 patients, single-arm. Sequencing is the question."
                "\n---\nSingle-arm.\n---\n"
            ),
            "note": "less hype",
            "category": "voice",
        },
    )
    assert r.status_code == 303
    dec = store.list_decisions(conn, did)[0]
    assert dec["category"] == "voice"
    body = client.get(f"/drafts/{did}").text
    assert '<pre class="diff">' in body
    assert '<span class="del">- post 1: A game-changer' in body
    assert '<span class="add">+ post 1: ORR 88% in 97 patients, single-arm.' in body
    assert '<span class="same">  post 2: Single-arm.' in body
    assert "<td>voice</td>" in body and "less hype" in body


def test_decision_diff_marks_removed_and_added_lines():
    diff = decision_diff(
        store._serialise_text(["old post", "a", "b"]), store._serialise_text(["new post", "a"])
    )
    assert ("del", "post 1: old post") in diff
    assert ("add", "post 1: new post") in diff
    assert ("", "post 2: a") in diff
    assert ("del", "post 3: b") in diff
    assert not any(line.startswith("?") for _, line in diff)


def test_detail_without_edit_has_no_diff(client, conn):
    did = _draft(conn)
    client.post(f"/drafts/{did}/approve", data={"note": "fine"})
    body = client.get(f"/drafts/{did}").text
    assert '<pre class="diff">' not in body and "fine" in body


def test_voice_page_without_data(client):
    r = client.get("/voice")
    assert r.status_code == 200
    body = r.text
    assert "Voice report" in body and "n/a" in body
    assert "No edits yet" in body and "Nothing to propose yet" in body
    assert "Caveats" in body and "draft/voice.md" in body
    assert client.get("/voice?weeks=0").status_code == 400


def test_voice_page_with_data(client, conn):
    did = _draft(conn, source="biorxiv")
    client.post(
        f"/drafts/{did}/edit",
        data={
            "thread": (
                "Preprint: ORR 88% in 97 patients, single-arm, sequencing open."
                "\n---\nSingle-arm.\n---\n"
            ),
            "note": "less hype",
            "category": "voice",
        },
    )
    did2 = _draft(conn, item_id="i2")
    client.post(f"/drafts/{did2}/reject", data={"note": "not news", "category": "not_newsworthy"})
    r = client.get("/voice?weeks=2")
    assert r.status_code == 200
    body = r.text
    assert "last 2 weeks" in body
    assert "<td>biorxiv</td>" in body and "<td>voice</td>" in body
    assert "game-changer" in body  # banned phrase hit from the original
    assert "less hype" in body and "not news" in body
    assert f'href="/drafts/{did}"' in body  # top edit links to the draft
    assert "50%" in body  # edit rate: 1 edited of (0 + 1 + 1)


def test_index_links_to_voice_report(client):
    body = client.get("/queue").text
    assert 'href="/voice"' in body
