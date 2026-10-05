"""Chart kinds beyond plain bars (grouped arms, headline stat tiles), the flat-chart guard
and the story's company logo in the card header."""

import json

import pytest

from draft import branding
from draft.chart import (
    Chart,
    ChartError,
    alt_text,
    flat_chart_problems,
    render_chart,
    render_table,
    validate_chart,
    validate_table,
    visual_from_json,
)
from draft.drafter import chart_problems
from draft.schema import Draft

GROUPED = {
    "kind": "grouped",
    "title": "Drug vs control",
    "labels": ["ORR", "CR rate"],
    "series": [
        {"name": "Drug + chemo", "values": [62.5, 21]},
        {"name": "Chemo", "values": [40, 8]},
    ],
    "values": [],
    "unit": "%",
    "note": "n=310",
}
STAT = {
    "kind": "stat",
    "title": "Single-arm readout",
    "labels": ["ORR", "Median DOR"],
    "values": [73, 11.2],
    "units": ["%", "months"],
    "unit": "",
}


def test_grouped_chart_round_trips_and_lists_every_arm_value():
    chart = validate_chart(GROUPED)
    assert chart.kind == "grouped" and [s.name for s in chart.series] == ["Drug + chemo", "Chemo"]
    assert chart.numbers() == ["62.5%", "21%", "40%", "8%"]
    assert "Chemo" in chart.texts()
    again = visual_from_json(json.dumps(chart.to_dict()))
    assert again == chart
    assert "CR rate: Drug + chemo 21%, Chemo 8%" in alt_text(chart)


def test_stat_chart_has_a_unit_per_tile():
    chart = validate_chart(STAT)
    assert chart.numbers() == ["73%", "11.2"]
    assert chart.unit_at(1) == "months"
    assert "Median DOR 11.2 months" in alt_text(chart)
    assert validate_chart({**STAT, "labels": ["ORR"], "values": [73], "units": ["%"]})


def test_plain_bar_chart_json_is_unchanged():
    chart = validate_chart({"title": "t", "labels": ["a", "b"], "values": [1, 2], "unit": "%"})
    assert set(chart.to_dict()) == {"title", "labels", "values", "unit", "note"}


@pytest.mark.parametrize(
    "bad",
    [
        {**GROUPED, "series": GROUPED["series"][:1]},  # one arm
        {**GROUPED, "values": [1, 2]},  # values outside series
        {**GROUPED, "series": [{"name": "a", "values": [1]}, {"name": "b", "values": [2]}]},
        {**STAT, "units": ["%"]},  # units length
        {**STAT, "labels": list("abcde"), "values": [1, 2, 3, 4, 5], "units": []},  # 5 tiles
        {"title": "t", "labels": ["a"], "values": [1], "unit": ""},  # one bar
        {"title": "t", "labels": ["a", "b"], "values": [1, 2], "unit": "", "kind": "pie"},
        {"title": "t", "labels": ["a", "b"], "values": [1, 2], "unit": "", "units": ["%", "%"]},
    ],
)
def test_malformed_kinds_are_refused(bad):
    with pytest.raises(ChartError):
        validate_chart(bad)


def test_flat_bar_chart_is_sent_back():
    flat = Chart("Both in phase 3", ["A", "B"], [3, 3])
    assert flat_chart_problems(flat)
    assert not flat_chart_problems(Chart("t", ["A", "B"], [3, 4]))
    assert not flat_chart_problems(validate_chart({**STAT, "values": [5, 5]}))
    draft = Draft(
        thread=["x"], why_it_matters="", suggested_visual="", claims_to_verify=[], chart=flat
    )
    problems = chart_problems(draft, "Both arms are in phase 3 studies.")
    assert len(problems) == 1 and "all equal" in problems[0]


def test_grouped_numbers_are_checked_against_the_source():
    draft = Draft(
        thread=["x"],
        why_it_matters="",
        suggested_visual="",
        claims_to_verify=[],
        chart=validate_chart(GROUPED),
    )
    source = "ORR 62.5% vs 40%; CR rate 21% vs 8% in 310 patients."
    assert chart_problems(draft, source) == []
    assert "21%" in chart_problems(draft, source.replace("21%", "20%"))[0]


def test_render_each_kind_with_a_header_logo(tmp_path):
    pytest.importorskip("matplotlib")
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    logo = tmp_path / "logo.png"
    fig = plt.figure(figsize=(2, 1))
    fig.savefig(logo)
    plt.close(fig)
    for name, spec in (("g", GROUPED), ("s", STAT)):
        path = render_chart(validate_chart(spec), tmp_path / f"{name}.png", header_logo=logo)
        assert path.stat().st_size > 1000
    table = validate_table({"title": "t", "columns": ["A", "B"], "rows": [["a", "1"], ["b", "2"]]})
    # an unreadable logo is skipped, never an error
    assert render_table(table, tmp_path / "t.png", header_logo=tmp_path / "nope.png").exists()


CFG = {
    "companies": {
        "feeds": [
            {
                "key": "regeneron",
                "name": "Regeneron",
                "url": "https://investor.regeneron.com/rss",
            },
            {
                "key": "autolus",
                "name": "Autolus",
                "url": "https://autolus.gcs-web.com/rss",
                "domain": "autolus.com",
            },
            {"key": "crispr", "name": "CRISPR", "url": "https://crisprtx.gcs-web.com/rss"},
        ]
    },
    "branding": {"logos_dir": "logos", "companies": [{"key": "amgen", "name": "Amgen"}]},
}


def test_story_logo_from_the_source_site_then_the_title(tmp_path):
    (tmp_path / "logos").mkdir()
    for key in ("regeneron", "autolus", "amgen", "crispr"):
        (tmp_path / "logos" / f"{key}.png").write_bytes(b"x")
    b = branding.load_branding(CFG, tmp_path)
    logo = branding.story_logo
    assert logo(b, "Anything", "https://investor.regeneron.com/news/1").name == "regeneron.png"
    assert logo(b, "Anything", "https://www.autolus.com/x").name == "autolus.png"
    # a feed on a hosted IR platform claims its own host, never the platform's other tenants
    assert b.by_host("https://crisprtx.gcs-web.com/news").key == "crispr"
    assert b.by_host("https://other.gcs-web.com/x") is None
    assert logo(b, "Amgen bispecific readout", "https://nejm.org/x").name == "amgen.png"
    assert logo(b, "Amgenix is not Amgen's", "").name == "amgen.png"
    assert logo(b, "No company here", "https://nejm.org/x") is None


def test_brand_chart_leaves_other_kinds_alone():
    b = branding.load_branding(CFG, ".")
    chart = validate_chart({**GROUPED, "labels": ["Amgen", "Regeneron"]})
    assert branding.brand_chart(chart, b) == (chart, {})
