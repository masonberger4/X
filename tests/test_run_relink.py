"""The one-off pass that moves a single or long post's source URL into a link post."""

import run_relink
from approval_queue import store
from draft.chart import Chart
from draft.hook import split_link_post
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


def test_split_link_post_moves_the_url_and_its_lead_in():
    assert split_link_post(f"ORR 88% held up. Full results: {URL}", URL) == (
        "ORR 88% held up.",
        f"Source: {URL}",
    )
    # a URL mid-sentence leaves the writer's own words alone
    body, link = split_link_post(f"ORR 88% {URL} in the phase 2", URL)
    assert body == "ORR 88% in the phase 2." and link == f"Source: {URL}"
    # nothing usable left, and nothing to move
    assert split_link_post(URL, URL) is None
    assert split_link_post("no link here", URL) is None


def test_dry_run_reports_and_changes_nothing(conn, caplog):
    did = _insert(conn, "i1", f"ORR 88% held up. {URL}")
    with caplog.at_level("INFO"):
        assert run_relink.relink(conn, [store.STATUS_PENDING], dry_run=True) == 1
    assert store.get_draft(conn, did).draft.thread == [f"ORR 88% held up. {URL}"]
    assert "would become" in caplog.text


def test_a_single_post_gets_its_link_post(conn):
    did = _insert(conn, "i1", f"ORR 88% held up. Source: {URL}")
    assert run_relink.relink(conn, [store.STATUS_PENDING], dry_run=False) == 1
    row = store.get_draft(conn, did)
    assert row.draft.thread == ["ORR 88% held up.", f"Source: {URL}"]
    assert row.status == store.STATUS_PENDING  # a relink is never an approval
    rows = conn.execute("SELECT note, original_text, edited_text FROM decisions").fetchall()
    assert rows and rows[0]["note"] == run_relink.NOTE
    assert URL in rows[0]["original_text"] and f"Source: {URL}" in rows[0]["edited_text"]
    # running it again finds nothing left to do
    assert run_relink.relink(conn, [store.STATUS_PENDING], dry_run=False) == 0


def test_a_long_post_keeps_its_sections(conn):
    body = "Section one.\n\nSection two."
    did = _insert(conn, "i1", f"{body}\n\nSource: {URL}", long_format(4000))
    assert run_relink.relink(conn, [store.STATUS_PENDING], dry_run=False) == 1
    assert store.get_draft(conn, did).draft.thread == [body, f"Source: {URL}"]


def test_a_thread_and_an_already_split_draft_are_left_alone(conn):
    seed_item(conn, "i1")
    thread = Draft(["hook", "middle", f"last {URL}"], "v", "w", chart=CHART)
    store.insert_draft(conn, item_id="i1", model="m", draft=thread)
    _insert(conn, "i2", "already split", single_format())
    assert run_relink.relink(conn, [store.STATUS_PENDING], dry_run=False) == 0
    assert conn.execute("SELECT count(*) c FROM decisions").fetchone()["c"] == 0


def test_a_post_that_is_only_the_url_is_reported_not_split(conn, caplog):
    _insert(conn, "i1", URL)
    with caplog.at_level("WARNING"):
        assert run_relink.relink(conn, [store.STATUS_PENDING], dry_run=False) == 0
    assert "left alone" in caplog.text
