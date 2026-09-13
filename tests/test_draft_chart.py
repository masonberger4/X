"""draft/chart.py: the chart spec the model returns, its verification, and the PNG."""

import json

import pytest

from draft import chart as chartmod
from draft.chart import Chart, ChartError, alt_text, format_value, render_chart, validate_chart
from draft.drafter import DraftRejected, chart_problems, verify_chart
from draft.schema import SchemaError, validate_output
from tests.test_draft_drafter import ABSTRACT, URL, fake_call, good_json, run

CHART = {
    "title": "Phase 2 outcomes",
    "labels": ["ORR", "Grade 3 CRS"],
    "values": [88, 4.1],
    "unit": "%",
    "note": "n=97, single arm",
}


def test_format_value():
    assert format_value(88.0, "%") == "88%"
    assert format_value(14.6) == "14.6"
    assert format_value(1200.0) == "1200"


def test_validate_chart_accepts_null_and_rejects_shape():
    assert validate_chart(None) is None
    c = validate_chart(CHART)
    assert c.labels == ["ORR", "Grade 3 CRS"] and c.values == [88.0, 4.1] and c.unit == "%"
    assert c.numbers() == ["88%", "4.1%"]
    with pytest.raises(ChartError):
        validate_chart({**CHART, "values": [88]})  # length mismatch
    with pytest.raises(ChartError):
        validate_chart({**CHART, "labels": ["one"], "values": [1]})  # < 2 bars
    with pytest.raises(ChartError):
        validate_chart({**CHART, "values": ["88", "4"]})
    with pytest.raises(ChartError):
        validate_chart({**CHART, "extra": 1})
    with pytest.raises(ChartError):
        validate_chart("a waterfall plot")


def test_validate_output_requires_exactly_one_visual():
    d = validate_output(good_json(chart=CHART))
    assert isinstance(d.chart, Chart) and d.chart.title == "Phase 2 outcomes"
    assert d.to_dict()["chart"]["values"] == [88.0, 4.1]
    with pytest.raises(SchemaError):
        validate_output(good_json(chart={"title": "x"}))
    # no visual at all is a schema error, whether the key is null or absent
    with pytest.raises(SchemaError, match="every draft needs a visual"):
        validate_output(good_json(chart=None))
    data = good_json()
    del data["chart"]
    with pytest.raises(SchemaError, match="every draft needs a visual"):
        validate_output(data)
    table = {"title": "t", "columns": ["Asset", "Phase"], "rows": [["a", "1"], ["b", "2"]]}
    assert validate_output(good_json(chart=None, table=table)).table is not None
    with pytest.raises(SchemaError, match="not both"):
        validate_output(good_json(table=table))


def test_chart_numbers_are_checked_against_the_source():
    d = validate_output(good_json(chart=CHART))
    assert verify_chart(d, ABSTRACT) == []
    bad = validate_output(good_json(chart={**CHART, "values": [88, 5.0]}))
    assert verify_chart(bad, ABSTRACT) == ["5%"]
    # a source that writes "4.0%" still verifies the JSON number 4 (2 and 2.0 are the same)
    whole = validate_output(good_json(chart={**CHART, "values": [88, 4]}))
    assert verify_chart(whole, ABSTRACT.replace("4.1%", "4.0%")) == []
    assert verify_chart(whole, ABSTRACT) == ["4%"]
    # numbers hidden in the title, labels or note are checked too
    bad2 = validate_output(good_json(chart={**CHART, "note": "n=120"}))
    assert verify_chart(bad2, ABSTRACT) == ["120"]


def test_unverified_chart_is_a_retry_reason_then_a_rejection():
    bad = good_json(chart={**CHART, "values": [88, 5.0]})
    d = validate_output(bad)
    (problem,) = chart_problems(d, ABSTRACT)
    assert problem.startswith("chart number(s) not in the source: 5%")
    # the model is told and gets another go
    call = fake_call([bad, good_json(chart=CHART)])
    result = run(call)
    assert result.attempts == 2 and result.draft.chart is not None
    assert "chart number(s) not in the source: 5%" in call.calls[1][1]
    assert result.flagged_numbers == [] and result.draft.claims_to_verify == []
    # never fixed: the draft is rejected, not stored without its picture
    with pytest.raises(DraftRejected) as exc:
        run(fake_call([bad, bad]), max_attempts=2)
    assert any(r.startswith("chart number(s) not in the source") for r in exc.value.reasons)


def test_verified_chart_survives_drafting():
    result = run(fake_call([good_json(chart=CHART)]))
    assert result.draft.chart is not None and result.attempts == 1
    d = validate_output(good_json(chart=CHART))
    assert chart_problems(d, ABSTRACT) == [] and d.chart is not None


def test_alt_text_reads_every_bar_and_is_capped():
    c = validate_chart(CHART)
    text = alt_text(c, URL)
    assert text == (
        "Bar chart: Phase 2 outcomes. ORR 88%; Grade 3 CRS 4.1%. n=97, single arm. Source: doi.org."
    )
    long = validate_chart({**CHART, "note": "x" * 2000})
    assert len(alt_text(long)) == chartmod.MAX_ALT_TEXT


def test_render_chart_writes_a_png(tmp_path):
    pytest.importorskip("matplotlib")
    path = render_chart(validate_chart(CHART), tmp_path / "sub" / "c.png", source_url=URL)
    data = path.read_bytes()
    assert data[:8] == b"\x89PNG\r\n\x1a\n" and len(data) > 1000


def test_chart_from_json_round_trip_and_garbage():
    c = validate_chart(CHART)
    assert chartmod.chart_from_json(json.dumps(c.to_dict())) == c
    assert chartmod.chart_from_json(None) is None
    assert chartmod.chart_from_json("not json") is None
    assert chartmod.chart_from_json('{"title": "x"}') is None
