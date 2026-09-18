"""The one-off pass that takes the source URL out of the posts of queued drafts."""

import run_unlink
from approval_queue import store
from draft.chart import Chart
from draft.hook import strip_links
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
