"""The one-off pass that takes the source URL out of the posts of queued drafts."""

import run_unlink
from approval_queue import store
from draft.chart import Chart
from draft.hook import link_problems, strip_links
from draft.schema import Draft, long_format, single_format
from tests.conftest import URL, seed_item

CHART = Chart("t", ["A", "B"], [88.0, 4.1], "%")


def _draft(text, fmt):
    d = Draft([text], "v", "w", chart=CHART)
    d.shape, d.anchors, d.max_chars, d.wanted_visuals = fmt.shape, [1], fmt.max_chars, 1
    return d


def _insert(conn, item, text, fmt=None, status=store.STATUS_PENDING):
    seed_item(conn, item)
    return store.insert_draft(
        conn,
        item_id=item,
        model="m",
        draft=_draft(text, fmt or single_format()),
        status=status,
    )


def test_strip_links_removes_the_url_and_its_lead_in():
    assert strip_links(f"ORR 88% held up. Full results: {URL}") == "ORR 88% held up."
    # a URL mid-sentence leaves the writer's own words alone
    assert strip_links(f"ORR 88% {URL} in the phase 2") == "ORR 88% in the phase 2."
    # a bare domain is a link too
    assert strip_links("ORR 88% held up. nejm.org/doi/x") == "ORR 88% held up."
    # nothing usable left, and nothing to strip
    assert strip_links(URL) is None
    assert strip_links("no link here") is None


def test_dry_run_reports_and_changes_nothing(conn, caplog):
    did = _insert(conn, "i1", f"ORR 88% held up. {URL}")
    with caplog.at_level("INFO"):
        assert run_unlink.unlink(conn, [store.STATUS_PENDING], dry_run=True) == 1
    assert store.get_draft(conn, did).draft.thread == [f"ORR 88% held up. {URL}"]
    assert "would become" in caplog.text


def test_a_single_post_loses_its_link(conn):
    did = _insert(conn, "i1", f"ORR 88% held up. Source: {URL}")
    assert run_unlink.unlink(conn, [store.STATUS_PENDING], dry_run=False) == 1
    row = store.get_draft(conn, did)
    assert row.draft.thread == ["ORR 88% held up."]
    assert row.status == store.STATUS_PENDING  # an unlink is never an approval
    rows = conn.execute("SELECT note, original_text, edited_text FROM decisions").fetchall()
    assert rows and rows[0]["note"] == run_unlink.NOTE
    assert URL in rows[0]["original_text"] and URL not in rows[0]["edited_text"]
    # running it again finds nothing left to do
    assert run_unlink.unlink(conn, [store.STATUS_PENDING], dry_run=False) == 0


def test_a_long_post_keeps_its_sections(conn):
    body = "Section one.\n\nSection two."
    did = _insert(conn, "i1", f"{body}\n\nSource: {URL}", long_format(4000))
    assert run_unlink.unlink(conn, [store.STATUS_PENDING], dry_run=False) == 1
    assert store.get_draft(conn, did).draft.thread == [body]


def test_a_thread_loses_its_link_post(conn):
    seed_item(conn, "i1")
    thread = Draft(["hook", "middle", f"Source: {URL}"], "v", "w", chart=CHART)
    did = store.insert_draft(conn, item_id="i1", model="m", draft=thread)
    assert run_unlink.unlink(conn, [store.STATUS_PENDING], dry_run=False) == 1
    assert store.get_draft(conn, did).draft.thread == ["hook", "middle"]


def test_a_link_free_draft_is_left_alone(conn):
    _insert(conn, "i1", "already link-free", single_format())
    assert run_unlink.unlink(conn, [store.STATUS_PENDING], dry_run=False) == 0
    assert conn.execute("SELECT count(*) c FROM decisions").fetchone()["c"] == 0


def test_a_draft_that_is_only_a_url_is_reported_not_changed(conn, caplog):
    _insert(conn, "i1", URL)
    with caplog.at_level("WARNING"):
        assert run_unlink.unlink(conn, [store.STATUS_PENDING], dry_run=False) == 0
    assert "left alone" in caplog.text


def test_a_studio_draft_is_left_alone(conn, caplog):
    # A studio post may name a foreign ticker the drafter's bare-domain test reads as a link;
    # the studio's own checker already blocks real links, so its text is never touched here.
    text = "Swiss pharma is where the cash sits. Roche (ROG.SW/RHHBY) has said so."
    assert link_problems(text)  # what made run_unlink strip it
    did = store.insert_draft(
        conn,
        item_id=store.studio_item_id(3),
        model="opus (studio)",
        draft=_draft(text, long_format(25000)),
    )
    store.approve(conn, did)
    with caplog.at_level("INFO"):
        assert run_unlink.unlink(conn, [store.STATUS_PENDING, store.STATUS_APPROVED], False) == 0
    assert store.get_draft(conn, did).draft.thread == [text]
    assert f"draft {did}: a studio piece, left alone" in caplog.text


def test_a_draft_already_on_x_is_left_alone(conn, caplog):
    from publish import store as publish_store

    did = _insert(conn, "i1", f"ORR 88% held up. {URL}")
    store.approve(conn, did)
    publish_store.connect().close()  # step 3's tables
    assert publish_store.record_manual(conn, did, [f"ORR 88% held up. {URL}"])
    with caplog.at_level("INFO"):
        assert run_unlink.unlink(conn, [store.STATUS_APPROVED], dry_run=False) == 0
    assert store.get_draft(conn, did).draft.thread == [f"ORR 88% held up. {URL}"]
    assert "posted or being posted, left alone" in caplog.text
