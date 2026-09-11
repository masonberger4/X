"""Comparison tables: the spec, the pure decision rules, the run_verify table pass with a
fake model (source-backed cells skip the web, blanked cells, drops), the stored alt text,
the queue page, approval and revision. No network."""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

import run_verify
from approval_queue import images, store
from approval_queue.app import app
from draft import drafter
from draft.chart import BLANK_CELL, Table, alt_text, validate_table, visual_from_json
from draft.schema import Draft, SchemaError, validate_output
from publish import store as pub_store
from tests.conftest import URL, seed_item
from verify import store as vstore
from verify import tables
from verify.verifier import ClaimCheck

pytest.importorskip("matplotlib")

TABLE = {
    "title": "Next-generation CTLA-4 programs",
    "columns": ["Company", "Asset", "Stage"],
    "rows": [
        ["Solstice", "porustobart", "phase 2"],  # "phase 2" is in the conftest abstract
        ["Agenus", "botensilimab", "Phase 3 planned"],
        ["Xilio", "vilastobart", "Phase 2"],
    ],
    "note": "as disclosed",
}


def _draft(table=None):
    return Draft(f"ORR 88% {URL}", ["a", "b", f"c {URL}"], "landscape", "w", table=table)


def _seed(conn, item_id="i1", table=TABLE):
    seed_item(conn, item_id)
    return store.insert_draft(conn, item_id=item_id, model="m", draft=_draft(validate_table(table)))


# --- spec -----------------------------------------------------------------------------


def test_validate_table_shape_and_round_trip():
    t = validate_table(TABLE)
    assert t.columns == ["Company", "Asset", "Stage"] and len(t.cells()) == 9
    assert visual_from_json(json.dumps(t.to_dict())) == t
    assert visual_from_json(
        json.dumps({"title": "c", "labels": ["a", "b"], "values": [1, 2], "unit": ""})
    ).labels == ["a", "b"]
    for bad, msg in [
        ({**TABLE, "columns": ["only"]}, "columns"),
        ({**TABLE, "rows": [["a", "b"]]}, "rows"),
        ({**TABLE, "rows": [["", "x", "y"], ["a", "b", "c"]]}, "empty row label"),
        ({**TABLE, "rows": [["a", "x" * 61, "y"], ["a", "b", "c"]]}, "over"),
        ({**TABLE, "extra": 1}, "unexpected"),
    ]:
        with pytest.raises(ValueError, match=msg):
            validate_table(bad)
    assert validate_table(None) is None


def test_schema_accepts_a_table_but_not_both_visuals():
    out = {
        "single_post": "p",
        "thread": ["a", "b", "c"],
        "suggested_visual": "",
        "why_it_matters": "",
        "claims_to_verify": [],
        "table": TABLE,
    }
    d = validate_output(out)
    assert d.table.title == TABLE["title"] and d.visual is d.table
    out["chart"] = {"title": "c", "labels": ["a", "b"], "values": [1, 2], "unit": ""}
    with pytest.raises(SchemaError, match="not both"):
        validate_output(out)


def test_hard_rules_scan_table_cells():
    t = validate_table({**TABLE, "rows": [["A", "x", "buy the stock"], ["B", "y", "z"]]})
    problems = drafter.check_hard_rules(_draft(t), url=URL, source="pubmed")
    assert any("table reads as investment advice" in p for p in problems)


def test_alt_text_and_render_blank_cells(tmp_path):
    t = validate_table(TABLE)
    blanked = frozenset({(1, 2)})
    alt = alt_text(t, URL, blanked)
    assert alt.startswith(
        "Table: Next-generation CTLA-4 programs. Solstice: Asset porustobart; Stage phase 2."
    )
    assert "Agenus: Asset botensilimab; Stage n/a." in alt and "Source: doi.org." in alt
    from draft.chart import render_table

    path = render_table(t, tmp_path / "t.png", source_url=URL, blanked=blanked)
    assert path.stat().st_size > 1000 and BLANK_CELL  # drawn without error


# --- decision rules --------------------------------------------------------------------


def test_source_backed_cells_and_cell_claims():
    t = validate_table(TABLE)
    # case-insensitive, so both "phase 2" and "Phase 2" are backed by the article
    assert tables.source_backed_cells(t, "In this phase 2 trial ... 88%") == [(0, 2), (2, 2)]
    assert tables.source_backed_cells(t, "88% of 97") == []
    assert tables.cell_claim(t, 1, 2) == "Agenus, Stage: Phase 3 planned"
    assert tables.cell_claim(t, 1, 0).startswith("Company: Agenus")


def _verdicts(t, verdict="supported", trusted=True, overrides=None):
    out = []
    for r, c, _ in t.cells():
        v, tr = (overrides or {}).get((r, c), (verdict, trusted))
        out.append(tables.CellVerdict(r, c, v, tr))
    return out


def test_decide_pending_render_and_drop_rules():
    t = validate_table(TABLE)
    kw = dict(min_supported_ratio=0.6, max_cells=30)
    assert tables.decide(t, [], **kw).status == tables.PENDING
    assert len(tables.decide(t, [], **kw).unchecked) == 9
    d = tables.decide(t, _verdicts(t), **kw)
    assert d.status == tables.RENDER and d.blanked == frozenset()
    # unverified or untrusted cells are blanked, not fatal
    d = tables.decide(
        t,
        _verdicts(t, overrides={(1, 2): ("unverified", False), (2, 1): ("supported", False)}),
        **kw,
    )
    assert d.status == tables.RENDER and d.blanked == {(1, 2), (2, 1)}
    # a contradicted cell drops the table
    d = tables.decide(t, _verdicts(t, overrides={(0, 1): ("contradicted", True)}), **kw)
    assert d.status == tables.DROP and "porustobart" in d.reason
    # too few supported fact cells drops it
    labels_ok = {(r, 0): ("supported", True) for r in range(3)}
    d = tables.decide(t, _verdicts(t, "unverified", False, overrides=labels_ok), **kw)
    assert d.status == tables.DROP and "0 of 6 cells" in d.reason
    # a blanked row label removes the row; under two rows left drops the table
    d = tables.decide(t, _verdicts(t, overrides={(0, 0): ("unverified", False)}), **kw)
    assert d.status == tables.RENDER
    drawn = tables.apply_row_drops(t, d.blanked)
    assert [r[0] for r in drawn.rows] == ["Agenus", "Xilio"]
    d = tables.decide(
        t,
        _verdicts(t, overrides={(0, 0): ("unverified", False), (1, 0): ("unverified", False)}),
        **kw,
    )
    assert d.status == tables.DROP and "rows" in d.reason
    assert tables.decide(t, [], min_supported_ratio=0.6, max_cells=4).status == tables.DROP


# --- run_verify table pass -------------------------------------------------------------


def _fake_verify(monkeypatch, verdict_for):
    calls = []

    def fake(index, claim, **kw):
        calls.append(claim)
        v, host = verdict_for(claim)
        return ClaimCheck(
            index,
            claim,
            v,
            f"https://{host}/x" if host else "",
            "q",
            "n",
            bool(host) and host == "sec.gov",
        )

    monkeypatch.setattr(run_verify, "verify_claim", fake)
    monkeypatch.setattr(run_verify, "_root_config", lambda: {})
    return calls


def test_run_verify_checks_cells_blanks_one_and_renders(conn, monkeypatch):
    did = _seed(conn)

    def verdict_for(claim):
        if "Phase 3 planned" in claim:
            return "unverified", ""
        return "supported", "sec.gov"

    calls = _fake_verify(monkeypatch, verdict_for)
    assert run_verify.main(["--dry-run"]) == 0 and calls == []
    assert run_verify.main([]) == 0
    # the two source-backed "phase 2" cells never went to the web
    assert len(calls) == 7 and not any(c.lower().endswith(": phase 2") for c in calls)
    checks = vstore.table_checks_for_draft(conn, did)
    assert len(checks) == 9
    src = next(k for k in checks if (k.row, k.col) == (2, 2))
    assert src.model == tables.SOURCE_MODEL and src.trusted and src.source_url == URL
    row = store.get_draft(conn, did)
    assert row.image_path == f"draft_{did}.png"
    assert "Agenus: Asset botensilimab; Stage n/a." in row.image_alt
    assert "Xilio: Asset vilastobart; Stage Phase 2." in row.image_alt
    # publish sees the same picture and the same alt text
    store.approve(conn, did)
    (a,) = pub_store.fetch_approved(conn=conn)
    assert a.image_path.endswith(f"draft_{did}.png") and a.image_alt == row.image_alt
    # nothing left to do
    calls.clear()
    run_verify.main([])
    assert calls == []


def test_run_verify_drops_a_contradicted_table_on_the_record(conn, monkeypatch):
    did = _seed(conn)
    _fake_verify(
        monkeypatch,
        lambda claim: (
            ("contradicted", "sec.gov") if "Agenus, Stage" in claim else ("supported", "sec.gov")
        ),
    )
    run_verify.main([])
    row = store.get_draft(conn, did)
    assert row.draft.table is None and row.image_path is None
    (dec,) = store.list_decisions(conn, did)
    assert dec["action"] == "edit" and "contradicted" in dec["note"] and "Agenus" in dec["note"]


def test_run_verify_survives_a_failed_cell_and_finishes_next_run(conn, monkeypatch):
    did = _seed(conn)
    state = {"fail": True}

    def verdict_for(claim):
        if state["fail"] and "Agenus, Asset" in claim:
            raise RuntimeError("boom")
        return "supported", "sec.gov"

    calls = _fake_verify(monkeypatch, verdict_for)
    run_verify.main([])
    assert store.get_draft(conn, did).image_path is None  # one cell still unchecked
    state["fail"] = False
    calls.clear()
    run_verify.main([])
    assert calls == ["Agenus, Asset: botensilimab"]
    assert store.get_draft(conn, did).image_path == f"draft_{did}.png"


def test_tables_can_be_disabled(conn, monkeypatch):
    did = _seed(conn)
    calls = _fake_verify(monkeypatch, lambda c: ("supported", "sec.gov"))
    cfg = dict(run_verify.load_verify_config())
    cfg["tables"] = {**cfg["tables"], "enabled": False}
    monkeypatch.setattr(run_verify, "load_verify_config", lambda: cfg)
    run_verify.main([])
    assert calls == [] and store.get_draft(conn, did).image_path is None


# --- queue -----------------------------------------------------------------------------


@pytest.fixture
def client(db_file):
    return TestClient(app, follow_redirects=False)


def test_queue_shows_cells_and_approving_early_drops_the_table(client, conn):
    did = _seed(conn)
    vstore.insert_table_check(
        conn,
        did,
        1,
        2,
        "Phase 3 planned",
        ClaimCheck(0, "c", "unverified", "", "", "thin", False),
        "m",
    )
    body = client.get(f"/drafts/{did}").text
    assert "Table cells" in body and "1 of 9 cells checked" in body
    assert "Approving before then posts the text alone" in body
    assert 'class="cell unverified"' in body and 'class="cell unchecked"' in body
    r = client.post(f"/drafts/{did}/approve", data={})
    assert r.status_code == 303
    row = store.get_draft(conn, did)
    assert row.status == "approved" and row.draft.table is None
    notes = [d["note"] for d in store.list_decisions(conn, did)]
    assert any("not verified when the draft was approved" in n for n in notes)


def test_revise_keeps_cell_verdicts_for_unchanged_cells(client, conn, monkeypatch):
    did = _seed(conn)
    for (r, c), v in {(0, 1): "supported", (1, 2): "unverified", (2, 2): "supported"}.items():
        vstore.insert_table_check(
            conn,
            did,
            r,
            c,
            TABLE["rows"][r][c],
            ClaimCheck(0, "c", v, "https://sec.gov/x", "q", "n", True),
            "m",
        )
    out = {
        "single_post": f"ORR 88% {URL}",
        "thread": ["a", "b", f"c {URL}"],
        "suggested_visual": "landscape",
        "why_it_matters": "w",
        "claims_to_verify": [],
        "table": {
            **TABLE,
            "rows": [
                ["Xilio", "vilastobart", "Phase 2"],  # moved: verdict follows the cell
                ["Solstice", "porustobart", "phase 2"],
                ["Agenus", "botensilimab", "Phase 3 (ROBBIN)"],  # changed: checked again
            ],
        },
    }
    monkeypatch.setattr(drafter, "call_anthropic", lambda s, u, m: json.dumps(out))
    monkeypatch.setattr(drafter, "model_name", lambda: "stub")
    assert client.post(f"/drafts/{did}/revise", data={"instructions": "reorder"}).status_code == 303
    checks = {(k.row, k.col): k.verdict for k in vstore.table_checks_for_draft(conn, did)}
    assert checks == {(0, 2): "supported", (1, 1): "supported"}
    # a revision without a table forgets the cell verdicts
    out["table"] = None
    client.post(f"/drafts/{did}/revise", data={"instructions": "drop it"})
    assert vstore.table_checks_for_draft(conn, did) == []


def test_attach_chart_leaves_a_table_for_the_verifier(conn):
    did = _seed(conn)
    assert images.attach_chart(conn, did, store.get_draft(conn, did).draft.table) is None
    assert store.get_draft(conn, did).image_path is None
    t = Table("T", ["a", "b"], [["x", "y"], ["z", ""]])
    assert images.attach_table(conn, did, t, blanked=frozenset({(0, 1)})) is not None
    assert store.get_draft(conn, did).image_alt == "Table: T. x: b n/a. z: b n/a."
