"""draft/chart.py: the chart spec the model returns, its verification, and the PNG."""

import json

import pytest

from draft import chart as chartmod
from draft.chart import ChartError, format_value, validate_chart

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


def test_chart_from_json_round_trip_and_garbage():
    c = validate_chart(CHART)
    assert chartmod.chart_from_json(json.dumps(c.to_dict())) == c
    assert chartmod.chart_from_json(None) is None
    assert chartmod.chart_from_json("not json") is None
    assert chartmod.chart_from_json('{"title": "x"}') is None
