import json

import pytest
from fastapi.testclient import TestClient

from approval_queue import store
from approval_queue.app import app
from draft import drafter
from draft.schema import Claim, Draft
from tests.conftest import URL, seed_item
from verify import store as verify_store
from verify.verifier import ClaimCheck


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
    r = client.get("/queue")
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
    assert r.status_code == 303 and r.headers["location"] == "/queue"
    row = store.get_draft(conn, draft_id)
    assert row.status == "approved"
    dec = store.list_decisions(conn, draft_id)
    assert dec[0]["action"] == "approve" and dec[0]["note"] == "good"
    assert f"/drafts/{draft_id}" not in client.get("/queue").text
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
    assert f"/drafts/{draft_id}" not in client.get("/queue").text
    assert f"/drafts/{draft_id}" in client.get("/status/snoozed").text


def test_actions_on_missing_draft_404(client):
    for action in ("approve", "reject", "snooze"):
        assert client.post(f"/drafts/999/{action}").status_code == 404
    assert (
        client.post("/drafts/999/edit", data={"single_post": "s", "thread": ""}).status_code == 404
    )
    assert client.get("/status/bogus").status_code == 404


# --- step 7 -----------------------------------------------------------------------


def test_edit_form_accepts_category_and_rejects_unknown(client, conn, draft_id):
    data = {"single_post": f"Preprint, edited. {URL}", "thread": f"Preprint. a\n---\nb {URL}"}
    assert (
        client.post(f"/drafts/{draft_id}/edit", data={**data, "category": "x"}).status_code == 400
    )
    assert store.list_decisions(conn, draft_id) == []
    r = client.post(f"/drafts/{draft_id}/edit", data={**data, "category": "factual"})
    assert r.status_code == 303
    assert store.list_decisions(conn, draft_id)[0]["category"] == "factual"


def test_voice_route_exists(client, draft_id):
    assert client.get("/voice").status_code == 200


# --- revise (AI rewrite on the human's note) ----------------------------------------


def _revision_json(single_post):
    return json.dumps(
        {
            "single_post": single_post,
            "thread": ["Preprint. r1", "r2", f"r3 {URL}"],
            "suggested_visual": "",
            "why_it_matters": "revised",
            "claims_to_verify": [{"claim": "new claim", "confidence": "medium"}],
        }
    )


def _stub_call(monkeypatch, responses):
    calls = []

    def fake(system, user, model):
        calls.append((system, user, model))
        r = responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r

    monkeypatch.setattr(drafter, "call_anthropic", fake)
    monkeypatch.setattr(drafter, "model_name", lambda: "stub-model")
    return calls


def test_revise_rewrites_draft_from_instructions_and_keeps_pending(
    client, conn, draft_id, monkeypatch
):
    calls = _stub_call(monkeypatch, [_revision_json(f"Preprint: tighter. ORR 88%. {URL}")])
    r = client.post(
        f"/drafts/{draft_id}/revise", data={"instructions": "tighter opening", "category": "voice"}
    )
    assert r.status_code == 303 and r.headers["location"] == f"/drafts/{draft_id}?revised=1"
    row = store.get_draft(conn, draft_id)
    assert row.status == "pending"
    assert row.draft.single_post.startswith("Preprint: tighter.")
    assert row.draft.thread[0] == "Preprint. r1"
    assert [c.claim for c in row.draft.claims_to_verify] == ["new claim"]
    assert row.model == "stub-model"
    (dec,) = store.list_decisions(conn, draft_id)
    assert dec["action"] == "revise" and dec["note"] == "tighter opening"
    assert dec["category"] == "voice"
    system, user, model = calls[0]
    assert "tighter opening" in user and "Preprint: ORR 88%." in user
    assert model == "stub-model"
    body = client.get(f"/drafts/{draft_id}?revised=1").text
    assert "Revised." in body and "revise" in body
    assert "- single: Preprint: ORR 88%." in body  # diff of the AI rewrite is shown


def test_revise_with_empty_box_fixes_failed_claims_and_resets_checks(
    client, conn, draft_id, monkeypatch
):
    verify_store.insert_check(
        conn,
        draft_id,
        ClaimCheck(0, "Number '15'...", "contradicted", "https://src", "it was 14", "wrong", True),
        "checker",
    )
    calls = _stub_call(monkeypatch, [_revision_json(f"Preprint: fixed. ORR 88%. {URL}")])
    r = client.post(f"/drafts/{draft_id}/revise", data={"instructions": ""})
    assert r.status_code == 303
    user = calls[0][1]
    assert "FACT-CHECK FAILURES" in user and "it was 14" in user and "https://src" in user
    assert verify_store.checks_for_draft(conn, draft_id) == []  # re-verified next run
    (dec,) = store.list_decisions(conn, draft_id)
    assert dec["note"] == "fix fact-check failures"
    assert not verify_store.has_contradiction(conn, draft_id)


def test_revise_keeps_supported_verdicts_for_unchanged_claims(client, conn, draft_id, monkeypatch):
    verify_store.insert_check(
        conn,
        draft_id,
        ClaimCheck(0, "ORR was 88%.", "supported", "https://src", "88%", "", True),
        "checker",
    )
    verify_store.insert_check(
        conn, draft_id, ClaimCheck(1, "n=40", "unverified", "", "", "thin", False), "checker"
    )
    verify_store.insert_check(
        conn,
        draft_id,
        ClaimCheck(2, "Phase 3", "supported", "https://src", "ph3", "", True),
        "checker",
    )
    body = json.loads(_revision_json(f"Preprint: tighter. ORR 88%. {URL}"))
    body["claims_to_verify"] = [
        {"claim": "n=40", "confidence": "low"},
        {"claim": "orr was 88%", "confidence": "high"},  # same claim, new position and case
        {"claim": "Phase 3 in 2027", "confidence": "low"},  # changed text: checked again
    ]
    _stub_call(monkeypatch, [json.dumps(body)])
    assert client.post(f"/drafts/{draft_id}/revise", data={"instructions": "x"}).status_code == 303
    checks = verify_store.checks_for_draft(conn, draft_id)
    assert [(c.claim_index, c.claim, c.verdict) for c in checks] == [
        (1, "orr was 88%", "supported")
    ]
    assert checks[0].source_url == "https://src" and checks[0].trusted
    row = store.get_draft(conn, draft_id)
    assert verify_store.unchecked_indexes(conn, row) == [0, 2]


def test_revise_with_nothing_to_do_or_failed_model_leaves_draft_alone(
    client, conn, draft_id, monkeypatch
):
    r = client.post(f"/drafts/{draft_id}/revise", data={"instructions": ""})
    assert r.status_code == 303 and "error=" in r.headers["location"]
    _stub_call(monkeypatch, [json.dumps({"single_post": "no url"})] * drafter.MAX_ATTEMPTS)
    r = client.post(f"/drafts/{draft_id}/revise", data={"instructions": "x"})
    assert r.status_code == 303 and "error=" in r.headers["location"]
    _stub_call(monkeypatch, [RuntimeError("api down")] * drafter.MAX_ATTEMPTS)
    monkeypatch.setattr(drafter, "_sleep_backoff", lambda a, s: None)
    r = client.post(f"/drafts/{draft_id}/revise", data={"instructions": "x"})
    assert r.status_code == 303 and "api%20down" in r.headers["location"]
    row = store.get_draft(conn, draft_id)
    assert row.draft.single_post == f"Preprint: ORR 88%. {URL}"
    assert store.list_decisions(conn, draft_id) == []
    assert client.post("/drafts/999/revise", data={"instructions": "x"}).status_code == 404
    body = client.get(f"/drafts/{draft_id}?error=api%20down").text
    assert "api down" in body


def test_pending_page_has_inline_revise_box(client, draft_id):
    body = client.get("/queue").text
    assert f'action="/drafts/{draft_id}/revise"' in body
    assert 'action="/drafts/' not in client.get("/status/approved").text
