"""The one-off pass that clears picture captions written to the operator."""

import pytest

import run_scrub_notes
from approval_queue import store
from draft.chart import Chart, Table
from draft.schema import Draft
from tests.conftest import seed_item

BAD = "Status as of Sept 2026; verify each cell against current FDA labels before posting"
GOOD = "n=97, single arm"


def _draft(visual):
    kind = {"chart": visual} if isinstance(visual, Chart) else {"table": visual}
    return Draft(["ORR 88%", "b", "c"], "v", "w", **kind)


class _NoClose:
    """The CLI closes the connection it opened; the test fixture owns this one."""

    def __init__(self, conn):
        self._conn = conn

    def __getattr__(self, name):
        return getattr(self._conn, name)

    def close(self):
        pass


def _insert(conn, item, visual):
    seed_item(conn, item)
    return store.insert_draft(conn, item_id=item, model="m", draft=_draft(visual))


def test_dry_run_reports_and_changes_nothing(conn, caplog):
    did = _insert(conn, "i1", Chart("t", ["A", "B"], [88.0, 4.1], "%", note=BAD))
    with caplog.at_level("INFO"):
        assert run_scrub_notes.scrub(conn, [store.STATUS_PENDING], dry_run=True) == 1
    assert store.get_draft(conn, did).draft.chart.note == BAD
    assert "would clear" in caplog.text


def test_a_chart_caption_is_cleared_and_the_rest_of_the_spec_kept(conn):
    did = _insert(conn, "i1", Chart("t", ["A", "B"], [88.0, 4.1], "%", note=BAD))
    assert run_scrub_notes.scrub(conn, [store.STATUS_PENDING], dry_run=False) == 1
    chart = store.get_draft(conn, did).draft.chart
    assert chart.note == ""
    assert chart.labels == ["A", "B"] and chart.values == [88.0, 4.1]
    # the clearing is on the record as an edit whose text did not change
    rows = conn.execute("SELECT note, original_text, edited_text FROM decisions").fetchall()
    assert rows and rows[0]["original_text"] == rows[0]["edited_text"]
    assert rows[0]["note"] == run_scrub_notes.NOTE


def test_a_factual_caption_is_left_alone(conn):
    did = _insert(conn, "i1", Chart("t", ["A", "B"], [88.0, 4.1], "%", note=GOOD))
    assert run_scrub_notes.scrub(conn, [store.STATUS_PENDING], dry_run=False) == 0
    assert store.get_draft(conn, did).draft.chart.note == GOOD
    assert conn.execute("SELECT count(*) c FROM decisions").fetchone()["c"] == 0


def test_a_table_caption_is_cleared(conn):
    table = Table("Landscape", ["Asset", "Phase"], [["a", "1"], ["b", "2"]], note=BAD)
    did = _insert(conn, "i1", table)
    assert run_scrub_notes.scrub(conn, [store.STATUS_PENDING], dry_run=False) == 1
    assert store.get_draft(conn, did).draft.table.note == ""


def test_the_cli_runs_over_the_configured_statuses(conn, monkeypatch):
    did = _insert(conn, "i1", Chart("t", ["A", "B"], [88.0, 4.1], "%", note=BAD))
    store.approve(conn, did)
    monkeypatch.setattr(store, "connect", lambda *a, **k: _NoClose(conn))
    assert run_scrub_notes.main(["--status", "approved"]) == 0
    assert store.get_draft(conn, did).draft.chart.note == ""


@pytest.mark.parametrize("flag", ["--dry-run", "-v"])
def test_the_cli_accepts_its_flags(conn, monkeypatch, flag):
    monkeypatch.setattr(store, "connect", lambda *a, **k: _NoClose(conn))
    assert run_scrub_notes.main([flag]) == 0
