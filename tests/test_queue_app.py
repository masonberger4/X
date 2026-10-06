from datetime import UTC, datetime, timedelta
from urllib.parse import unquote

import pytest
from fastapi.testclient import TestClient

from approval_queue import app as queue_app
from approval_queue import publishing, store
from approval_queue.app import app
from draft.schema import Claim, Draft
from publish import store as publish_store
from tests.conftest import seed_item


@pytest.fixture
def client(db_file):
    # db_file sets DB_PATH so the app's per-request connections hit the temp file
    return TestClient(app, follow_redirects=False)


@pytest.fixture
def draft_id(conn):
    seed_item(conn, "i1", source="biorxiv")
    d = Draft(
        thread=["Preprint. ORR 88%. one", "two", "three"],
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


def test_detail_shows_post_thread_and_lengths(client, draft_id):
    r = client.get(f"/drafts/{draft_id}")
    assert r.status_code == 200
    body = r.text
    assert "1/3 Preprint. ORR 88%. one" in body and "3/3 three" in body
    assert "Single post" not in body
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


def test_approve_action_no_table_has_no_notice(client, conn, draft_id):
    r = client.post(f"/drafts/{draft_id}/approve", data={"note": "good"})
    assert r.status_code == 303 and r.headers["location"] == "/queue"
    assert "notice=" not in r.headers["location"]
    row = store.get_draft(conn, draft_id)
    assert row.status == "approved"


def test_edit_action_saves_original_and_edited(client, conn, draft_id):
    r = client.post(
        f"/drafts/{draft_id}/edit",
        data={
            "thread": "Preprint, edited. first\n---\nsecond\n---\nlast",
            "note": "tightened",
        },
    )
    assert r.status_code == 303
    row = store.get_draft(conn, draft_id)
    assert row.status == "approved"
    assert row.draft.thread == ["Preprint, edited. first", "second", "last"]
    dec = store.list_decisions(conn, draft_id)[0]
    assert dec["action"] == "edit"
    assert "ORR 88%" in dec["original_text"]
    assert "edited" in dec["edited_text"]
    assert dec["note"] == "tightened"


def test_edit_keep_pending(client, conn, draft_id):
    r = client.post(
        f"/drafts/{draft_id}/edit",
        data={"thread": "a\n---\nb\n---\nc", "keep_pending": "1"},
    )
    assert r.status_code == 303 and r.headers["location"] == f"/drafts/{draft_id}"
    assert store.get_draft(conn, draft_id).status == "pending"


def test_edit_rejects_over_280_and_empty(client, conn, draft_id):
    r = client.post(f"/drafts/{draft_id}/edit", data={"thread": "x" * 281 + "\n---\nb"})
    assert r.status_code == 400 and "280" in r.text
    # The refusal is the detail page itself, error on top and the typed text kept, with a
    # way back; never FastAPI's bare JSON page.
    assert "Not saved" in r.text and "post 1 is 281 characters" in r.text
    assert "x" * 281 in r.text and 'href="/queue"' in r.text
    assert "<details open>" in r.text
    r = client.post(f"/drafts/{draft_id}/edit", data={"thread": ""})
    assert r.status_code == 400 and "cannot be empty" in r.text
    assert store.get_draft(conn, draft_id).status == "pending"
    assert store.list_decisions(conn, draft_id) == []


def test_reject_action(client, conn, draft_id):
    r = client.post(f"/drafts/{draft_id}/reject", data={"note": "wrong take"})
    assert r.status_code == 303
    assert store.get_draft(conn, draft_id).status == "rejected"
    assert store.list_decisions(conn, draft_id)[0]["note"] == "wrong take"
    assert f"/drafts/{draft_id}" in client.get("/status/rejected").text


def test_reopen_action_sends_an_approved_draft_back_to_pending(client, conn, draft_id):
    client.post(f"/drafts/{draft_id}/approve")
    r = client.post(f"/drafts/{draft_id}/reopen", data={"note": "second thoughts"})
    assert r.status_code == 303 and r.headers["location"] == "/status/approved"
    assert store.get_draft(conn, draft_id).status == "pending"
    assert f"/drafts/{draft_id}" in client.get("/queue").text
    assert f"/drafts/{draft_id}" not in client.get("/status/approved").text
    dec = store.list_decisions(conn, draft_id)[-1]
    assert dec["action"] == "reopen" and dec["note"] == "second thoughts"


def test_actions_on_missing_draft_404(client):
    for action in ("approve", "reject", "reopen"):
        assert client.post(f"/drafts/999/{action}").status_code == 404
    assert client.post("/drafts/999/edit", data={"thread": "s"}).status_code == 404
    assert client.get("/status/bogus").status_code == 404
    assert client.get("/status/snoozed").status_code == 404


# --- step 7 -----------------------------------------------------------------------


def test_edit_form_accepts_category_and_rejects_unknown(client, conn, draft_id):
    data = {"thread": "Preprint, edited. a\n---\nb"}
    assert (
        client.post(f"/drafts/{draft_id}/edit", data={**data, "category": "x"}).status_code == 400
    )
    assert store.list_decisions(conn, draft_id) == []
    r = client.post(f"/drafts/{draft_id}/edit", data={**data, "category": "factual"})
    assert r.status_code == 303
    assert store.list_decisions(conn, draft_id)[0]["category"] == "factual"


def test_approved_page_offers_reopen_but_not_approve_reject_or_revise(client, conn, draft_id):
    store.approve(conn, draft_id)
    body = client.get("/status/approved").text
    assert f'action="/drafts/{draft_id}/reopen"' in body and ">Reopen<" in body
    for action in ("approve", "reject", "revise", "edit"):
        assert f'action="/drafts/{draft_id}/{action}"' not in body
    # "Publish now" belongs to the panel; the standalone queue never offers it. Ask for the
    # standalone globals rather than assuming them: importing panel.app sets the panel's on
    # this same environment, for the whole process (conftest puts back whatever we change).
    queue_app.install_standalone_globals()
    assert 'action="/publishing/now"' not in client.get("/status/approved").text
    # once step 3 has it, neither button is offered any more
    _publish_draft(conn, draft_id, tweet_id="555")
    body = client.get("/status/approved?posted=1").text
    assert f'action="/drafts/{draft_id}/reopen"' not in body


def _publish_draft(conn, draft_id, *, status="posted", tweet_id="555", error=None):
    """Write what step 3 would: a schedule claim and a first post row."""
    from publish import store as pstore

    pstore.connect(conn.execute("PRAGMA database_list").fetchone()[2]).close()  # tables
    pstore.claim(conn, draft_id, "08:30")
    pstore.record_post(
        conn,
        draft_id=draft_id,
        text="one",
        kind="thread",
        position=1,
        slot="08:30",
        tweet_id=tweet_id,
        posted_at=None,
        error=error,
    )
    pstore.finish(conn, draft_id, status, error)


def test_approved_page_hides_posted_drafts_and_links_the_tweet(client, conn, draft_id):
    store.approve(conn, draft_id)
    seed_item(conn, "i2", source="pubmed")
    other = store.insert_draft(
        conn,
        item_id="i2",
        model="m",
        draft=Draft(thread=["Second"], suggested_visual="", why_it_matters=""),
    )
    store.approve(conn, other)
    # before any publish run: both listed as waiting, no tables yet
    body = client.get("/status/approved").text
    assert f"/drafts/{draft_id}" in body and f"/drafts/{other}" in body
    assert body.count("waiting") >= 2 and "already posted" not in body

    _publish_draft(conn, draft_id, tweet_id="555")
    body = client.get("/status/approved").text
    assert f"/drafts/{draft_id}" not in body and f"/drafts/{other}" in body
    assert "1 already posted, hidden" in body and "?posted=1" in body
    body = client.get("/status/approved?posted=1").text
    assert f"/drafts/{draft_id}" in body
    assert 'href="https://x.com/i/web/status/555"' in body and ">posted<" in body
    assert "Hide posted" in body
    # the detail page shows the same link
    detail = client.get(f"/drafts/{draft_id}").text
    assert "https://x.com/i/web/status/555" in detail and "published" in detail


def test_approved_page_shows_failed_and_partial_but_keeps_them(client, conn, draft_id):
    store.approve(conn, draft_id)
    _publish_draft(conn, draft_id, status="failed", tweet_id=None, error="HTTP 403")
    body = client.get("/status/approved").text
    assert f"/drafts/{draft_id}" in body and ">failed<" in body and 'title="HTTP 403"' in body
    conn.execute("UPDATE schedule SET status = 'partial' WHERE draft_id = ?", (draft_id,))
    conn.commit()
    body = client.get("/status/approved").text
    assert f"/drafts/{draft_id}" in body and "partial thread" in body
    # the pending list never shows publish pills
    assert "waiting" not in client.get("/queue").text


# --- reopen: what step 3 has already done to the draft --------------------------------


def _schedule_row(conn, draft_id):
    from publish import store as pstore

    return pstore.get_schedule(conn, draft_id)


def _claim_draft(conn, draft_id):
    """What step 3 does the moment it picks a draft up: a claimed schedule row, no posts."""
    from publish import store as pstore

    pstore.connect(conn.execute("PRAGMA database_list").fetchone()[2]).close()
    assert pstore.claim(conn, draft_id, "08:30")


@pytest.mark.parametrize("state", ["posted", "partial", "claimed"])
def test_reopen_refused_once_publishing_owns_the_draft(client, conn, draft_id, state):
    store.approve(conn, draft_id)
    if state == "claimed":
        _claim_draft(conn, draft_id)
    else:
        _publish_draft(conn, draft_id, status=state, tweet_id="555")
    r = client.post(f"/drafts/{draft_id}/reopen")
    assert r.status_code == 303 and r.headers["location"].startswith(f"/drafts/{draft_id}?error=")
    # the three refusals mean different things to the operator, so they must read differently
    said = unquote(r.headers["location"])
    assert ("already claimed" in said) if state == "claimed" else ("live on X" in said)
    assert store.get_draft(conn, draft_id).status == "approved"
    assert "reopen" not in [d["action"] for d in store.list_decisions(conn, draft_id)]
    # and the schedule row survives the refusal
    assert _schedule_row(conn, draft_id) is not None


@pytest.mark.parametrize("state", ["failed", "refused"])
def test_reopen_allowed_after_a_failed_or_refused_attempt(client, conn, draft_id, state):
    store.approve(conn, draft_id)
    _publish_draft(conn, draft_id, status=state, tweet_id=None, error="HTTP 403")
    r = client.post(f"/drafts/{draft_id}/reopen")
    assert r.status_code == 303 and r.headers["location"] == "/status/approved"
    assert store.get_draft(conn, draft_id).status == "pending"
    assert store.list_decisions(conn, draft_id)[-1]["action"] == "reopen"
    # the failed attempt's schedule row goes too, so the draft comes back clean
    assert publish_store.get_schedule(conn, draft_id) is None


def test_reopen_allowed_while_only_scheduled_or_with_no_schedule_row(client, conn, draft_id):
    from publish import store as pstore

    store.approve(conn, draft_id)
    seed_item(conn, "i2", source="pubmed")
    other = store.insert_draft(
        conn,
        item_id="i2",
        model="m",
        draft=Draft(thread=["Second"], suggested_visual="", why_it_matters=""),
    )
    store.approve(conn, other)
    pstore.connect(conn.execute("PRAGMA database_list").fetchone()[2]).close()
    pstore.set_order(conn, [draft_id])  # a saved publishing order, nothing claimed
    assert _schedule_row(conn, other) is None

    for did in (draft_id, other):
        r = client.post(f"/drafts/{did}/reopen")
        assert r.status_code == 303 and r.headers["location"] == "/status/approved"
        assert store.get_draft(conn, did).status == "pending"


@pytest.mark.parametrize("status", ["pending", "rejected"])
def test_reopen_refused_for_a_draft_that_is_not_approved(client, conn, draft_id, status):
    if status == "rejected":
        store.reject(conn, draft_id)
    r = client.post(f"/drafts/{draft_id}/reopen")
    assert r.status_code == 303 and "error=" in r.headers["location"]
    assert status in r.headers["location"]
    assert store.get_draft(conn, draft_id).status == status
    assert "reopen" not in [d["action"] for d in store.list_decisions(conn, draft_id)]


def test_reopen_releases_an_unclaimed_schedule_row_but_never_a_live_one(client, conn, draft_id):
    from publish import store as pstore

    store.approve(conn, draft_id)
    pstore.connect(conn.execute("PRAGMA database_list").fetchone()[2]).close()
    pstore.set_order(conn, [draft_id])
    assert _schedule_row(conn, draft_id)["position"] == 1
    assert client.post(f"/drafts/{draft_id}/reopen").status_code == 303
    # the saved order is gone, so re-approving does not resurrect it
    assert _schedule_row(conn, draft_id) is None

    # a posted draft keeps its row (the refusal never reaches release_unclaimed)
    store.approve(conn, draft_id)
    _publish_draft(conn, draft_id, tweet_id="555")
    assert client.post(f"/drafts/{draft_id}/reopen").status_code == 303
    assert _schedule_row(conn, draft_id) is not None
    # ... and so does a claimed one
    seed_item(conn, "i3", source="pubmed")
    other = store.insert_draft(
        conn,
        item_id="i3",
        model="m",
        draft=Draft(thread=["Third"], suggested_visual="", why_it_matters=""),
    )
    store.approve(conn, other)
    _claim_draft(conn, other)
    assert client.post(f"/drafts/{other}/reopen").status_code == 303
    assert _schedule_row(conn, other) is not None


def test_reopen_works_before_any_publish_run_has_made_step_3s_tables(client, conn, draft_id):
    store.approve(conn, draft_id)
    present = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "schedule" not in present and "posts" not in present
    r = client.post(f"/drafts/{draft_id}/reopen")
    assert r.status_code == 303 and r.headers["location"] == "/status/approved"
    assert store.get_draft(conn, draft_id).status == "pending"


def test_reopen_refused_for_a_draft_that_reached_x_without_a_schedule_row(client, conn, draft_id):
    """The posts log is the ground truth. A schedule row can be missing — deleted by hand,
    or lost to a release — while the tweets are still up, and the draft must stay put."""
    store.approve(conn, draft_id)
    _publish_draft(conn, draft_id, tweet_id="555")
    conn.execute("DELETE FROM schedule WHERE draft_id = ?", (draft_id,))
    conn.commit()
    assert store.publish_states(conn, [draft_id]) == {}  # schedule alone says: never posted
    r = client.post(f"/drafts/{draft_id}/reopen")
    assert r.status_code == 303 and "error=" in r.headers["location"]
    assert "live%20on%20X" in r.headers["location"]
    assert store.get_draft(conn, draft_id).status == "approved"
    assert "reopen" not in [d["action"] for d in store.list_decisions(conn, draft_id)]


def test_reopen_refused_for_a_schedule_state_it_does_not_know(client, conn, draft_id):
    """The guard is an allowlist: a state a later step 3 invents holds the draft instead of
    falling through to a reopen."""
    store.approve(conn, draft_id)
    _publish_draft(conn, draft_id, status="posting", tweet_id=None)
    r = client.post(f"/drafts/{draft_id}/reopen")
    assert r.status_code == 303 and "error=" in r.headers["location"]
    assert store.get_draft(conn, draft_id).status == "approved"


@pytest.mark.parametrize(
    "state,offered",
    [
        (None, True),
        ("pending", True),
        ("failed", True),
        ("refused", True),
        ("claimed", False),
        ("posted", False),
        ("partial", False),
    ],
)
def test_the_reopen_button_is_offered_exactly_where_the_route_allows_it(
    client, conn, draft_id, state, offered
):
    """The approved list, the draft's page and the route each decide whether a draft can be
    taken back. All three read PublishInfo.reopenable, and this pins them to one table."""
    store.approve(conn, draft_id)
    if state == "claimed":
        _claim_draft(conn, draft_id)
    elif state == "pending":
        publish_store.set_order(conn, [draft_id])
    elif state is not None:
        # failed and refused mean nothing reached X; posted and partial mean something did
        live = state in ("posted", "partial")
        _publish_draft(conn, draft_id, status=state, tweet_id="555" if live else None)
    form = f'action="/drafts/{draft_id}/reopen"'
    assert (form in client.get("/status/approved?posted=1").text) is offered
    assert (form in client.get(f"/drafts/{draft_id}").text) is offered
    # and the route agrees with the button
    client.post(f"/drafts/{draft_id}/reopen")
    assert (store.get_draft(conn, draft_id).status == "pending") is offered


def test_detail_shows_created_at_in_the_display_timezone(client, conn, draft_id):
    """A reviewer reads a Seattle clock, not the UTC string the row is stored as."""
    conn.execute(
        "UPDATE drafts SET created_at = ? WHERE id = ?",
        ("2026-06-01T15:30:00+00:00", draft_id),
    )
    conn.commit()
    body = client.get(f"/drafts/{draft_id}").text
    assert "created 2026-06-01 08:30 PDT" in body
    # The raw stored UTC string is never what the page shows.
    assert "2026-06-01T15:30:00+00:00" not in body


# --- release: a publish attempt that posted nothing ------------------------------------


def _age_claim(conn, draft_id, minutes):
    """Backdate a claim, as if the run that took it died `minutes` ago."""
    when = (datetime.now(UTC) - timedelta(minutes=minutes)).replace(microsecond=0)
    conn.execute(
        "UPDATE schedule SET claimed_at = ? WHERE draft_id = ?", (when.isoformat(), draft_id)
    )
    conn.commit()


STALE = publishing.STALE_CLAIM_MINUTES


def test_release_puts_a_draft_with_a_dead_claim_back_in_line(client, conn, draft_id):
    """The run that claimed this draft died without posting, so nothing will ever resolve
    the claim and fetch_approved skips it forever. Release drops that row: the draft stays
    approved and the next run considers it again."""
    store.approve(conn, draft_id)
    _claim_draft(conn, draft_id)
    _age_claim(conn, draft_id, STALE + 1)
    assert f'action="/drafts/{draft_id}/release"' in client.get("/status/approved").text

    r = client.post(f"/drafts/{draft_id}/release")
    assert r.status_code == 303 and r.headers["location"] == "/status/approved"
    assert store.get_draft(conn, draft_id).status == "approved"  # never a reopen
    assert _schedule_row(conn, draft_id) is None
    assert [a.draft_id for a in publish_store.fetch_approved(conn=conn)] == [draft_id]
    # and the page offers it again as waiting, not as releasable
    body = client.get("/status/approved").text
    assert "waiting" in body and f'action="/drafts/{draft_id}/release"' not in body


def test_release_refused_while_the_claim_could_still_be_posting(client, conn, draft_id):
    """A fresh claim is a run mid-thread, not a dead one: releasing it would post twice."""
    store.approve(conn, draft_id)
    _claim_draft(conn, draft_id)
    assert f'action="/drafts/{draft_id}/release"' not in client.get("/status/approved").text

    r = client.post(f"/drafts/{draft_id}/release")
    assert r.status_code == 303 and r.headers["location"].startswith(f"/drafts/{draft_id}?error=")
    assert f"less than {STALE} minutes ago" in unquote(r.headers["location"])
    assert _schedule_row(conn, draft_id)["status"] == "claimed"


@pytest.mark.parametrize("state", ["failed", "refused"])
def test_release_retries_a_failed_or_refused_attempt(client, conn, draft_id, state):
    """After retry.max_attempts the draft stays failed; this is the button for
    `run_publish.py --release-failed`, without taking the draft off the approved list."""
    store.approve(conn, draft_id)
    _publish_draft(conn, draft_id, status=state, tweet_id=None, error="HTTP 403")
    assert f'action="/drafts/{draft_id}/release"' in client.get("/status/approved").text

    r = client.post(f"/drafts/{draft_id}/release")
    assert r.status_code == 303 and r.headers["location"] == "/status/approved"
    assert store.get_draft(conn, draft_id).status == "approved"
    assert _schedule_row(conn, draft_id) is None
    assert [a.draft_id for a in publish_store.fetch_approved(conn=conn)] == [draft_id]


@pytest.mark.parametrize("state", ["posted", "partial"])
def test_release_refused_for_anything_live_on_x(client, conn, draft_id, state):
    """A partial thread is half on X; releasing it would post the whole thread again."""
    store.approve(conn, draft_id)
    _publish_draft(conn, draft_id, status=state, tweet_id="555")
    assert (
        f'action="/drafts/{draft_id}/release"' not in client.get("/status/approved?posted=1").text
    )

    r = client.post(f"/drafts/{draft_id}/release")
    assert "live on X" in unquote(r.headers["location"])
    assert _schedule_row(conn, draft_id)["status"] == state


def test_release_says_so_when_nothing_is_holding_the_draft(client, conn, draft_id):
    """A draft waiting with no claim (or only a saved publishing order) needs no release."""
    store.approve(conn, draft_id)
    body = client.get("/status/approved").text
    assert f'action="/drafts/{draft_id}/release"' not in body
    publish_store.set_order(conn, [draft_id])
    assert f'action="/drafts/{draft_id}/release"' not in client.get("/status/approved").text

    r = client.post(f"/drafts/{draft_id}/release")
    assert "not holding this draft" in unquote(r.headers["location"])
    assert _schedule_row(conn, draft_id)["position"] == 1  # the saved order survives


def test_release_refused_for_a_draft_that_is_not_approved(client, conn, draft_id):
    _claim_draft(conn, draft_id)
    _age_claim(conn, draft_id, STALE + 1)
    r = client.post(f"/drafts/{draft_id}/release")
    assert "only an approved draft" in unquote(r.headers["location"])
    assert _schedule_row(conn, draft_id)["status"] == "claimed"
    assert client.post("/drafts/999/release").status_code == 404


# ---- requests sent from another site ------------------------------------------------


@pytest.mark.parametrize(
    "headers",
    [
        {"Origin": "https://evil.example"},
        {"Origin": "http://testserver.evil.example"},
        {"Origin": "http://testserver:8001"},  # another app on the same machine
        {"Origin": "null"},  # a sandboxed frame or a data: URL
        {"Referer": "https://evil.example/a-page"},  # no Origin: the Referer decides
        {"Origin": "https://evil.example", "Referer": "http://testserver/queue"},
    ],
)
def test_a_form_sent_from_another_site_changes_nothing(client, conn, draft_id, headers):
    """No login: a web page open in the same browser could otherwise press any button."""
    r = client.post(f"/drafts/{draft_id}/reject", data={"note": "forged"}, headers=headers)
    assert r.status_code == 403
    assert "another site" in r.text and "Nothing was changed" in r.text
    assert store.get_draft(conn, draft_id).status == "pending"
    assert list(store.list_decisions(conn, draft_id)) == []


@pytest.mark.parametrize(
    "headers",
    [
        {},  # curl, a script on this machine
        {"Origin": "http://testserver"},
        {"Origin": "HTTP://TestServer"},
        {"Referer": "http://testserver/drafts/1?x=1"},
        {"Origin": "", "Referer": "http://testserver/queue"},
    ],
)
def test_the_apps_own_pages_and_requests_without_a_site_go_through(client, conn, draft_id, headers):
    r = client.post(f"/drafts/{draft_id}/reject", data={"note": "wrong take"}, headers=headers)
    assert r.status_code == 303
    assert store.get_draft(conn, draft_id).status == "rejected"


def test_pages_are_read_from_anywhere(client, draft_id):
    # a link from elsewhere opens a page; only a change is refused
    assert client.get("/queue", headers={"Origin": "https://evil.example"}).status_code == 200
    r = client.get(f"/drafts/{draft_id}", headers={"Referer": "https://evil.example/"})
    assert r.status_code == 200


@pytest.mark.parametrize(
    ("host", "sender", "refused"),
    [
        ("127.0.0.1:8000", "http://127.0.0.1:8000", False),
        ("localhost:8000", "http://127.0.0.1:8000", True),  # another name is another site
        ("[::1]:8000", "http://[::1]:8000", False),
        ("192.168.1.23:8000", "http://192.168.1.23:8000/queue", False),  # the phone on Wi-Fi
        ("my-pc:8000", "http://user@my-pc:8000", False),
        ("my-pc:8000", "http://my-pc:8000@evil.example", True),
        ("", "http://my-pc:8000", True),  # no Host: nothing to match
        ("my-pc:8000", "not a url", True),
    ],
)
def test_cross_site_compares_the_sending_host_with_the_requests_own(host, sender, refused):
    headers = {"host": host, "origin": sender}
    assert bool(queue_app.cross_site("POST", headers)) is refused
    assert queue_app.cross_site("GET", headers) == ""
    assert queue_app.cross_site("HEAD", headers) == ""
