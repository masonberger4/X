"""Chart images through the store, run_draft, the queue and the revise route."""

import json

import pytest
from fastapi.testclient import TestClient

from approval_queue import images, store
from approval_queue.app import app
from draft import drafter
from draft.chart import Chart
from draft.schema import Draft
from tests.conftest import URL, seed_item

pytest.importorskip("matplotlib")

# Only numbers that tests.conftest.ABSTRACT contains (88% and 14.6 months).
CHART = {
    "title": "Phase 2 outcomes",
    "labels": ["ORR", "Median PFS"],
    "values": [88, 14.6],
    "unit": "",
    "note": "n=97",
}


def _draft(chart=None):
    return Draft(f"ORR 88% {URL}", ["a", "b", f"c {URL}"], "v", "w", chart=chart)


def test_store_round_trips_chart_and_image(conn, tmp_path):
    seed_item(conn, "i1")
    chart = Chart("t", ["A", "B"], [88.0, 4.1], "%")
    did = store.insert_draft(conn, item_id="i1", model="m", draft=_draft(chart))
    row = store.get_draft(conn, did)
    assert row.draft.chart == chart and row.image_path is None
    # old databases get the columns through the guarded migration
    cols = {r[1] for r in conn.execute("PRAGMA table_info(drafts)")}
    assert {"chart_json", "image_path"} <= cols
    assert store.image_dir() == tmp_path / "images"
    path = images.attach_chart(conn, did, chart, source_url=URL)
    assert path == tmp_path / "images" / f"draft_{did}.png" and path.is_file()
    row = store.get_draft(conn, did)
    assert row.image_path == f"draft_{did}.png"
    assert store.resolve_image(row.image_path) == path
    store.drop_image(conn, did, note="wrong arm")
    row = store.get_draft(conn, did)
    assert row.draft.chart is None and row.image_path is None and not path.exists()
    dec = store.list_decisions(conn, did)
    assert dec[-1]["action"] == "edit" and dec[-1]["note"] == "wrong arm"
    assert row.draft.single_post == f"ORR 88% {URL}"  # text untouched


def test_attach_chart_respects_config_and_fails_soft(conn, monkeypatch, caplog):
    seed_item(conn, "i1")
    chart = Chart("t", ["A", "B"], [88.0, 4.1], "%")
    did = store.insert_draft(conn, item_id="i1", model="m", draft=_draft(chart))
    assert images.attach_chart(conn, did, None) is None
    assert images.attach_chart(conn, did, chart, cfg={"images": {"enabled": False}}) is None
    assert store.get_draft(conn, did).image_path is None

    def boom(*a, **k):
        raise ImportError("no matplotlib")

    monkeypatch.setattr(images, "render_chart", boom)
    assert images.attach_chart(conn, did, chart) is None
    assert "matplotlib is not installed" in caplog.text
    assert store.get_draft(conn, did).image_path is None


def test_run_draft_renders_the_chart(conn, monkeypatch, tmp_path):
    import run_draft

    seed_item(conn, "new", total=9.0)
    out = {
        "single_post": f"ORR 88% in 97 patients. {URL}",
        "thread": ["a", "b", f"c {URL}"],
        "suggested_visual": "v",
        "why_it_matters": "w",
        "claims_to_verify": [],
        "chart": CHART,
    }
    monkeypatch.setattr(
        run_draft,
        "draft_item",
        lambda **kw: drafter.draft_item(
            call=lambda s, u, m: json.dumps(out), sleep=lambda s: None, **kw
        ),
    )
    assert run_draft.main(["--min-score", "7"]) == 0
    row = store.list_drafts(conn)[0]
    assert row.draft.chart is not None
    assert (tmp_path / "images" / f"draft_{row.id}.png").is_file()
    assert row.image_path == f"draft_{row.id}.png"


@pytest.fixture
def client(db_file):
    return TestClient(app, follow_redirects=False)


def test_queue_serves_and_drops_the_image(client, conn):
    seed_item(conn, "i1")
    chart = Chart("Outcomes", ["ORR", "CRS"], [88.0, 4.1], "%", "n=97")
    did = store.insert_draft(conn, item_id="i1", model="m", draft=_draft(chart))
    assert client.get(f"/drafts/{did}/image").status_code == 404
    images.attach_chart(conn, did, chart, source_url=URL)
    r = client.get(f"/drafts/{did}/image")
    assert r.status_code == 200 and r.headers["content-type"] == "image/png"
    body = client.get(f"/drafts/{did}").text
    assert f"/drafts/{did}/image" in body and "Bar chart: Outcomes. ORR 88%; CRS 4.1%." in body
    assert "Drop image" in body
    r = client.post(f"/drafts/{did}/image/drop", data={"note": "no"})
    assert r.status_code == 303 and r.headers["location"] == f"/drafts/{did}"
    assert client.get(f"/drafts/{did}/image").status_code == 404
    assert "Drop image" not in client.get(f"/drafts/{did}").text
    assert client.post("/drafts/999/image/drop").status_code == 404


def test_revise_replaces_the_image(client, conn, monkeypatch):
    seed_item(conn, "i1")
    chart = Chart("Old", ["ORR", "CRS"], [88.0, 4.1], "%")
    did = store.insert_draft(conn, item_id="i1", model="m", draft=_draft(chart))
    old = images.attach_chart(conn, did, chart, source_url=URL)
    old_bytes = old.read_bytes()
    out = {
        "single_post": f"ORR 88% in 97 patients. {URL}",
        "thread": ["a", "b", f"c {URL}"],
        "suggested_visual": "v",
        "why_it_matters": "w",
        "claims_to_verify": [],
        "chart": {**CHART, "title": "New title"},
    }
    monkeypatch.setattr(drafter, "call_anthropic", lambda s, u, m: json.dumps(out))
    r = client.post(f"/drafts/{did}/revise", data={"instructions": "retitle"})
    assert r.status_code == 303
    row = store.get_draft(conn, did)
    assert row.draft.chart.title == "New title" and row.image_path == f"draft_{did}.png"
    assert old.read_bytes() != old_bytes
    # a revision without a chart leaves the draft text-only
    out["chart"] = None
    client.post(f"/drafts/{did}/revise", data={"instructions": "drop chart"})
    row = store.get_draft(conn, did)
    assert row.draft.chart is None and row.image_path is None


def test_redraw_button_remakes_a_chart(conn, db_file):
    seed_item(conn, "i1")
    chart = Chart("t", ["A", "B"], [88.0, 4.1], "%")
    did = store.insert_draft(conn, item_id="i1", model="m", draft=_draft(chart))
    path = images.attach_chart(conn, did, chart, source_url=URL)
    old = path.read_bytes()
    path.write_bytes(b"stale")
    client = TestClient(app, follow_redirects=False)
    r = client.post(f"/drafts/{did}/image/redraw")
    assert r.status_code == 303 and r.headers["location"] == f"/drafts/{did}"
    assert path.read_bytes() == old and store.get_draft(conn, did).draft.chart == chart
    # nothing to redraw: refused with a message, not an error page
    seed_item(conn, "i2")
    plain = store.insert_draft(conn, item_id="i2", model="m", draft=_draft())
    r = client.post(f"/drafts/{plain}/image/redraw")
    assert r.status_code == 303 and "error=" in r.headers["location"]
