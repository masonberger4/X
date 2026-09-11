"""Tickers and logos in table cells (draft/branding.py) and the 3D table header."""

import pytest

from draft import branding
from draft.chart import Style, Table, alt_text, render_table, validate_table

CFG = {
    "companies": {
        "feeds": [
            {"key": "amgen", "name": "Amgen", "url": "u", "ticker": "AMGN"},
            {"key": "merck", "name": "Merck", "url": "u", "ticker": "mrk"},
            {"key": "arsenal", "name": "Arsenal Bio", "url": "u"},
        ]
    },
    "branding": {
        "logos_dir": "logos",
        "companies": [
            {"key": "jnj", "name": "Johnson & Johnson", "ticker": "JNJ", "aliases": ["J&J"]},
            {"key": "boehringer", "name": "Boehringer Ingelheim"},
        ],
    },
}


def _table(*cells):
    return Table(
        "t", ["Program", "Sponsor", "Stage"], [[f"p{i}", c, "x"] for i, c in enumerate(cells)]
    )


def test_match_is_exact_or_suffixed_never_loose(tmp_path):
    b = branding.load_branding(CFG, tmp_path)
    assert b.match("Amgen").ticker == "AMGN" and b.match("Amgen Inc.").key == "amgen"
    assert b.match("The Amgen").key == "amgen" and b.match("Amgen ($AMGN)").key == "amgen"
    assert b.match("J&J").ticker == "JNJ" and b.match("johnson and johnson").key == "jnj"
    assert b.match("Merck").ticker == "MRK"  # normalised to upper case
    assert b.match("Merck KGaA") is None and b.match("Merck (via Harpoon)") is None
    assert b.match("Boehringer Ingelheim").ticker == "" and b.match("") is None
    assert b.match("Amgen and Kyowa") is None


def test_brand_table_appends_tickers_only_in_company_columns(tmp_path):
    (tmp_path / "logos").mkdir()
    (tmp_path / "logos" / "amgen.png").write_bytes(b"\x89PNG")
    b = branding.load_branding(CFG, tmp_path)
    t = _table("Amgen", "Amgen ($AMGN)", "Boehringer Ingelheim", "Unknown Co", "")
    branded, logos = branding.brand_table(t, b)
    assert [r[1] for r in branded.rows] == [
        "Amgen ($AMGN)",
        "Amgen ($AMGN)",
        "Boehringer Ingelheim",
        "Unknown Co",
        "",
    ]
    assert logos == {
        (0, 1): tmp_path / "logos" / "amgen.png",
        (1, 1): tmp_path / "logos" / "amgen.png",
    }
    assert t.rows[0][1] == "Amgen"  # the input table is untouched
    # a table with no company column is returned as is
    plain = Table("t", ["Asset", "Target"], [["a", "Amgen"]])
    assert branding.brand_table(plain, b) == (plain, {})
    assert b.is_company_column("Sponsor") and b.is_company_column("Lead company")
    assert not b.is_company_column("Companion diagnostic")


def test_load_branding_reads_the_shipped_config():
    import config as root_config

    b = branding.load_branding(root_config.load_config())
    assert b.match("Amgen").ticker == "AMGN" and b.match("Regeneron").ticker == "REGN"
    assert b.match("Johnson & Johnson").ticker == "JNJ"
    assert (
        b.match("Boehringer Ingelheim") is not None and b.match("Boehringer Ingelheim").ticker == ""
    )
    assert b.company_columns[:2] == ("company", "sponsor")


def test_render_table_draws_header_bar_and_logos(tmp_path):
    pytest.importorskip("matplotlib")
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    logo = tmp_path / "logo.png"
    fig = plt.figure(figsize=(1, 1))
    fig.savefig(logo)
    plt.close(fig)
    t = validate_table(
        {
            "title": "x",
            "columns": ["Program", "Company", "Stage"],
            "rows": [["a", "Amgen ($AMGN)", "p1"], ["b", "Other", "p2"], ["c", "Third", "p3"]],
        }
    )
    path = render_table(
        t,
        tmp_path / "t.png",
        blanked=frozenset({(1, 2)}),
        logos={(0, 1): logo, (2, 1): tmp_path / "missing.png"},
        style=Style(font_scale=1.2),
    )
    assert path.stat().st_size > 1000
    assert "Amgen ($AMGN)" in alt_text(t)


def test_attach_table_brands_from_root_config(conn, monkeypatch, tmp_path):
    pytest.importorskip("matplotlib")
    from approval_queue import images, store
    from draft.schema import Draft
    from tests.conftest import URL, seed_item

    seed_item(conn, "i1")
    t = _table("Amgen", "Unknown Co")
    did = store.insert_draft(
        conn,
        item_id="i1",
        model="m",
        draft=Draft(f"x {URL}", ["a", "b", f"c {URL}"], "v", "w", table=t),
    )
    seen = {}
    real = images.render_table

    def spy(table, path, **kw):
        seen["rows"] = table.rows
        seen["logos"] = kw.get("logos")
        return real(table, path, **kw)

    monkeypatch.setattr(images, "render_table", spy)
    images.attach_table(
        conn,
        did,
        t,
        source_url=URL,
        cfg={"images": {"enabled": True, "grader": {"enabled": False}}},
    )
    assert seen["rows"][0][1] == "Amgen ($AMGN)" and seen["rows"][1][1] == "Unknown Co"
    assert "Amgen ($AMGN)" in store.get_draft(conn, did).image_alt
    assert store.get_draft(conn, did).draft.table.rows[0][1] == "Amgen"  # spec untouched
