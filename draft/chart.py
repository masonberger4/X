"""A draft's visual spec: a chart or table, stored with the draft as JSON.

The retired drafter wrote these specs and rendered them; the studio draws its own cards,
so what is left here is the spec types, their validation and JSON round trip (drafts
already in the database still carry them) and the alt text the queue and step 3 show.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Any

IMAGES_DIRNAME = "images"  # <db folder>/images/draft_<id>.png; shared by steps 2 and 3

CHART_MIN_BARS = 2
CHART_MAX_BARS = 8
# The kinds of chart (Chart.kind). "bars": one value per label, the original. "grouped":
# two to four arms (series) side by side across one to six endpoints (labels), the shape of
# a randomised readout. "stat": one to four headline numbers drawn as big tiles, the shape
# of a single-arm readout (ORR, CR rate, median DOR), each with its own unit.
CHART_KINDS = ("bars", "grouped", "stat")
GROUPED_MIN_SERIES = 2
GROUPED_MAX_SERIES = 4
GROUPED_MAX_GROUPS = 6
STAT_MIN_TILES = 1
STAT_MAX_TILES = 4
MAX_ALT_TEXT = 1000  # X's limit for image alt text

# Rendered size: X shows a 16:9 image without cropping in the timeline.
WIDTH_PX = 1600
HEIGHT_PX = 900
DPI = 200

TABLE_MIN_ROWS = 2
TABLE_MAX_ROWS = 8
TABLE_MIN_COLS = 2
TABLE_MAX_COLS = 5
TABLE_MAX_CELL_CHARS = 60
BLANK_CELL = "—"  # what a cell the fact-checker could not support becomes in the picture

_HOST_RE = re.compile(r"^https?://(?:www\.)?([^/]+)")

# A note is a caption the reader sees under the picture. Phrases that address the operator
# or the reviewer ("verify each cell before posting", "internal use only") are an aside that
# leaked out of the drafting conversation and must never be rendered. Case-insensitive.
INTERNAL_NOTE_PATTERNS = (
    r"\bbefore (posting|publishing|you post|publication|tweeting)\b",
    r"\b(verify|check|confirm|double[- ]check|fact[- ]check|update|review)\b[^.;]{0,40}\bbefore\b",
    r"\b(please )?(verify|check|confirm|double[- ]check|fact[- ]check)\b[^.;]{0,20}"
    r"\b(each|every|all|the)\s+(cell|row|number|figure|value|claim|date)s?\b",
    r"\b(internal|draft|placeholder|do not post|not for publication|for review only|reviewer)\b",
    r"\bTODO\b",
    r"\bfill in\b",
)
_INTERNAL_NOTE_RE = re.compile("|".join(INTERNAL_NOTE_PATTERNS), re.IGNORECASE)


def note_problems(text: str, label: str) -> list[str]:
    """Hard-rule violations for a rendered caption: an aside meant for the operator, worded
    for the retry prompt. Empty list means the caption is fine to show a reader."""
    match = _INTERNAL_NOTE_RE.search(text or "")
    if match is None:
        return []
    return [
        f"{label} addresses the operator instead of the reader: {match.group(0)!r} "
        "(the note is printed under the picture for everyone to read; give a factual "
        "caption such as the n, the design or a caveat, or leave it empty)"
    ]


class ChartError(ValueError):
    """The chart spec is malformed (structure), as opposed to unverifiable (numbers)."""


@dataclass
class Series:
    """One arm of a grouped chart: its name and one value per endpoint (Chart.labels)."""

    name: str
    values: list[float]


@dataclass
class Chart:
    title: str
    labels: list[str]
    values: list[float]
    unit: str = ""  # "%", "months", "" ... shown on the axis and after each value
    note: str = ""  # one line under the chart, e.g. "n=97, single arm, investigator-assessed"
    kind: str = "bars"  # one of CHART_KINDS
    series: list[Series] = field(default_factory=list)  # "grouped": the arms; values is []
    units: list[str] = field(default_factory=list)  # "stat": a unit per tile ("" -> unit)

    def to_dict(self) -> dict[str, Any]:
        """The spec as stored in drafts.chart_json. A plain bar chart keeps the original
        five keys, so rows written before the other kinds read the same."""
        data = asdict(self)
        if self.kind == "bars":
            data.pop("kind")
        if not self.series:
            data.pop("series")
        if not self.units:
            data.pop("units")
        return data

    def unit_at(self, i: int) -> str:
        """The unit of value i: its own in `units` when given, else the chart's."""
        if i < len(self.units) and self.units[i].strip():
            return self.units[i].strip()
        return self.unit

    def numbers(self) -> list[str]:
        """Every value as the text a reader sees, with its % sign, for the verbatim check."""
        if self.kind == "grouped":
            return [format_value(v, self.unit) for s in self.series for v in s.values]
        return [format_value(v, self.unit_at(i)) for i, v in enumerate(self.values)]

    def texts(self) -> list[str]:
        """The free-text parts (title, labels, arm names, note), whose numbers are checked
        too."""
        return [self.title, *self.labels, *(s.name for s in self.series), self.note]


@dataclass
class Table:
    """A comparison table: `columns[0]` is the row label (company, asset, trial), the other
    columns are facts about it. Every cell except the headers is fact-checked before the
    table is rendered."""

    title: str
    columns: list[str]
    rows: list[list[str]]
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"kind": "table", **asdict(self)}

    def cells(self) -> list[tuple[int, int, str]]:
        """(row, col, text) for every non-empty body cell, row-major."""
        return [
            (r, c, cell)
            for r, row in enumerate(self.rows)
            for c, cell in enumerate(row)
            if cell.strip()
        ]


TABLE_JSON_SCHEMA: dict[str, Any] = {
    "type": ["object", "null"],
    "additionalProperties": False,
    "required": ["title", "columns", "rows"],
    "properties": {
        "title": {"type": "string", "description": "Table title, one line."},
        "columns": {
            "type": "array",
            "minItems": TABLE_MIN_COLS,
            "maxItems": TABLE_MAX_COLS,
            "items": {"type": "string"},
            "description": "Column headers; the first is the row label (company, asset...).",
        },
        "rows": {
            "type": "array",
            "minItems": TABLE_MIN_ROWS,
            "maxItems": TABLE_MAX_ROWS,
            "items": {
                "type": "array",
                "items": {"type": "string", "maxLength": TABLE_MAX_CELL_CHARS},
            },
            "description": (
                "One list of cells per row, same length as columns. Short factual cells "
                "(a stage, a date, a mechanism, a number); empty string when unknown."
            ),
        },
        "note": {
            "type": "string",
            "description": (
                "Optional footnote PRINTED UNDER THE TABLE for the reader: n, as-of date, "
                "design or caveat. Never an instruction to the operator or reviewer."
            ),
        },
    },
    "description": (
        "Optional comparison table (landscape, competitor set, catalyst list) to attach "
        "INSTEAD of a chart. Cells may use your own knowledge: every cell is fact-checked on "
        "the web before the table is drawn, unsupported cells are blanked and a contradicted "
        "cell drops the table. Null when a table would not add anything."
    ),
}


CHART_JSON_SCHEMA: dict[str, Any] = {
    "type": ["object", "null"],
    "additionalProperties": False,
    "required": ["title", "labels", "unit"],
    "properties": {
        "kind": {
            "type": "string",
            "enum": list(CHART_KINDS),
            "description": (
                "'bars' (default): one value per label, e.g. one endpoint across arms, "
                "doses, cohorts or competitors. 'grouped': two to four arms side by side "
                "across one to six endpoints (labels), e.g. drug vs control on ORR, CR and "
                "median PFS in a randomised readout; values go in 'series'. 'stat': one to "
                "four headline numbers drawn large, e.g. a single-arm ORR, CR rate and "
                "median DOR, each with its own unit in 'units'."
            ),
        },
        "title": {"type": "string", "description": "Chart title, one line."},
        "labels": {
            "type": "array",
            "minItems": 1,
            "maxItems": CHART_MAX_BARS,
            "items": {"type": "string"},
            "description": (
                "'bars': one label per bar (arm, dose, cohort, company). 'grouped': the "
                "endpoints, one per group. 'stat': the caption under each number."
            ),
        },
        "values": {
            "type": "array",
            "maxItems": CHART_MAX_BARS,
            "items": {"type": "number"},
            "description": (
                "One value per label, copied verbatim from the source. Empty for 'grouped'."
            ),
        },
        "series": {
            "type": "array",
            "maxItems": GROUPED_MAX_SERIES,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["name", "values"],
                "properties": {
                    "name": {"type": "string", "description": "The arm, e.g. 'Drug + chemo'."},
                    "values": {
                        "type": "array",
                        "items": {"type": "number"},
                        "description": "One value per label, verbatim from the source.",
                    },
                },
            },
            "description": "'grouped' only: the arms (experimental first, control last).",
        },
        "units": {
            "type": "array",
            "maxItems": STAT_MAX_TILES,
            "items": {"type": "string"},
            "description": (
                "'stat' only: the unit of each number ('%', 'months', 'patients'); '' uses 'unit'."
            ),
        },
        "unit": {"type": "string", "description": "'%', 'months', 'patients', or ''."},
        "note": {
            "type": "string",
            "description": (
                "Optional footnote PRINTED UNDER THE CHART for the reader: n, design, "
                "caveat. Never an instruction to the operator or reviewer."
            ),
        },
    },
    "description": (
        "Optional chart to attach, or null when the source has no numbers worth drawing. "
        "Only numbers that appear verbatim in the source; code checks each one and drops the "
        "chart otherwise."
    ),
}


def format_value(value: float, unit: str = "") -> str:
    """'88' for 88.0, '14.6' for 14.6, with '%' attached when that is the unit."""
    text = f"{value:g}" if isinstance(value, float) and not value.is_integer() else f"{int(value)}"
    if unit.strip() == "%":
        text += "%"
    return text


def validate_chart(data: Any) -> Chart | None:
    """Turn the model's `chart` value into a Chart, or None for null. Raises ChartError."""
    if data is None:
        return None
    if not isinstance(data, dict):
        raise ChartError("'chart' must be an object or null")
    extra = set(data) - set(CHART_JSON_SCHEMA["properties"])
    if extra:
        raise ChartError(f"chart has unexpected keys: {', '.join(sorted(extra))}")
    title = data.get("title")
    if not isinstance(title, str) or not title.strip():
        raise ChartError("chart.title must be a non-empty string")
    labels = data.get("labels")
    values = data.get("values")
    if not isinstance(labels, list) or not all(isinstance(x, str) and x.strip() for x in labels):
        raise ChartError("chart.labels must be a list of non-empty strings")
    if not isinstance(values, list) or not all(
        isinstance(v, int | float) and not isinstance(v, bool) for v in values
    ):
        raise ChartError("chart.values must be a list of numbers")
    unit = data.get("unit", "")
    note = data.get("note", "")
    if not isinstance(unit, str) or not isinstance(note, str):
        raise ChartError("chart.unit and chart.note must be strings")
    kind = data.get("kind") or "bars"
    if kind not in CHART_KINDS:
        raise ChartError(f"chart.kind must be one of {', '.join(CHART_KINDS)}")
    units = data.get("units") or []
    if not isinstance(units, list) or not all(isinstance(u, str) for u in units):
        raise ChartError("chart.units must be a list of strings")
    series: list[Series] = []
    if kind == "grouped":
        if values:
            raise ChartError("a grouped chart carries its values in 'series'; leave values empty")
        if not 1 <= len(labels) <= GROUPED_MAX_GROUPS:
            raise ChartError(f"a grouped chart needs 1-{GROUPED_MAX_GROUPS} endpoint labels")
        series = _validate_series(data.get("series"), len(labels))
    else:
        if data.get("series"):
            raise ChartError("'series' is for a grouped chart only")
        if len(labels) != len(values):
            raise ChartError("chart.labels and chart.values must have the same length")
        lo, hi = (
            (STAT_MIN_TILES, STAT_MAX_TILES) if kind == "stat" else (CHART_MIN_BARS, CHART_MAX_BARS)
        )
        if not lo <= len(labels) <= hi:
            raise ChartError(f"a {kind} chart needs {lo}-{hi} values, got {len(labels)}")
    if units and (kind != "stat" or len(units) != len(labels)):
        raise ChartError("'units' is for a stat chart only, one per label")
    return Chart(
        title=title.strip(),
        labels=[x.strip() for x in labels],
        values=[float(v) for v in values],
        unit=unit.strip(),
        note=note.strip(),
        kind=kind,
        series=series,
        units=[u.strip() for u in units],
    )


def _validate_series(data: Any, n_labels: int) -> list[Series]:
    """The arms of a grouped chart: 2-4, each named, each with one number per label."""
    if not isinstance(data, list) or not GROUPED_MIN_SERIES <= len(data) <= GROUPED_MAX_SERIES:
        raise ChartError(
            f"a grouped chart needs {GROUPED_MIN_SERIES}-{GROUPED_MAX_SERIES} series (arms)"
        )
    out: list[Series] = []
    for i, s in enumerate(data):
        if not isinstance(s, dict) or set(s) - {"name", "values"}:
            raise ChartError(f"chart.series[{i}] must be an object with name and values")
        name, vals = s.get("name"), s.get("values")
        if not isinstance(name, str) or not name.strip():
            raise ChartError(f"chart.series[{i}].name must be a non-empty string")
        if not isinstance(vals, list) or not all(
            isinstance(v, int | float) and not isinstance(v, bool) for v in vals
        ):
            raise ChartError(f"chart.series[{i}].values must be a list of numbers")
        if len(vals) != n_labels:
            raise ChartError(f"chart.series[{i}] needs one value per label ({n_labels})")
        out.append(Series(name=name.strip(), values=[float(v) for v in vals]))
    return out


def flat_chart_problems(chart: Chart) -> list[str]:
    """A bar chart whose bars are all the same height compares nothing (two arms 'in
    phase 3' drawn as two bars of 3). Worded for the retry prompt."""
    if chart.kind == "bars" and len(set(chart.values)) == 1:
        return [
            "chart bars are all equal, so the chart compares nothing; chart an efficacy or "
            "safety number that differs between arms, use a 'stat' chart for one arm's "
            "headline numbers, or give a table"
        ]
    return []


def validate_table(data: Any) -> Table | None:
    """Turn the model's `table` value into a Table, or None for null. Raises ChartError."""
    if data is None:
        return None
    if not isinstance(data, dict):
        raise ChartError("'table' must be an object or null")
    extra = set(data) - set(TABLE_JSON_SCHEMA["properties"]) - {"kind"}
    if extra:
        raise ChartError(f"table has unexpected keys: {', '.join(sorted(extra))}")
    title = data.get("title")
    if not isinstance(title, str) or not title.strip():
        raise ChartError("table.title must be a non-empty string")
    columns = data.get("columns")
    if not isinstance(columns, list) or not all(isinstance(x, str) and x.strip() for x in columns):
        raise ChartError("table.columns must be a list of non-empty strings")
    if not TABLE_MIN_COLS <= len(columns) <= TABLE_MAX_COLS:
        raise ChartError(
            f"table needs {TABLE_MIN_COLS}-{TABLE_MAX_COLS} columns, got {len(columns)}"
        )
    rows = data.get("rows")
    if not isinstance(rows, list) or not all(isinstance(r, list) for r in rows):
        raise ChartError("table.rows must be a list of lists")
    if not TABLE_MIN_ROWS <= len(rows) <= TABLE_MAX_ROWS:
        raise ChartError(f"table needs {TABLE_MIN_ROWS}-{TABLE_MAX_ROWS} rows, got {len(rows)}")
    clean: list[list[str]] = []
    for i, row in enumerate(rows):
        if len(row) != len(columns) or not all(isinstance(x, str) for x in row):
            raise ChartError(f"table.rows[{i}] must have {len(columns)} string cells")
        cells = [x.strip() for x in row]
        if not cells[0]:
            raise ChartError(f"table.rows[{i}] has an empty row label")
        if any(len(x) > TABLE_MAX_CELL_CHARS for x in cells):
            raise ChartError(f"table.rows[{i}] has a cell over {TABLE_MAX_CELL_CHARS} chars")
        clean.append(cells)
    note = data.get("note", "")
    if not isinstance(note, str):
        raise ChartError("table.note must be a string")
    return Table(
        title=title.strip(), columns=[x.strip() for x in columns], rows=clean, note=note.strip()
    )


def visual_from_json(text: str | None) -> Chart | Table | None:
    """The visual stored in drafts.chart_json: a Table when the JSON says kind=table, else a
    Chart (older rows have no kind). None when empty or unreadable."""
    import json

    if not text:
        return None
    try:
        data = json.loads(text)
        if isinstance(data, dict) and data.get("kind") == "table":
            return validate_table(data)
        return validate_chart(data)
    except (ValueError, TypeError):
        return None


def chart_from_json(text: str | None) -> Chart | None:
    """A Chart from drafts.chart_json; None when the row holds no chart (or holds a table)."""
    v = visual_from_json(text)
    return v if isinstance(v, Chart) else None


def table_from_json(text: str | None) -> Table | None:
    v = visual_from_json(text)
    return v if isinstance(v, Table) else None


def alt_text(
    chart: Chart | Table, source_url: str = "", blanked: frozenset[tuple[int, int]] = frozenset()
) -> str:
    """Alt text for the image: the title and every bar (or row), so it reads without the
    picture. For a table, `blanked` cells read as 'n/a' like they do in the picture."""
    if isinstance(chart, Table):
        rows = []
        for r, row in enumerate(chart.rows):
            facts = "; ".join(
                f"{col} {'n/a' if (r, c) in blanked or not cell else cell}"
                for c, (col, cell) in enumerate(zip(chart.columns, row, strict=True))
                if c > 0
            )
            rows.append(f"{row[0]}: {facts}")
        parts = [f"Table: {chart.title}. " + ". ".join(rows) + "."]
    elif chart.kind == "grouped":
        unit = f" {chart.unit}" if chart.unit and chart.unit != "%" else ""
        groups = "; ".join(
            f"{lab}: "
            + ", ".join(
                f"{s.name} {format_value(s.values[i], chart.unit)}{unit}" for s in chart.series
            )
            for i, lab in enumerate(chart.labels)
        )
        parts = [f"Grouped bar chart: {chart.title}. {groups}."]
    elif chart.kind == "stat":
        tiles = []
        for i, (lab, val) in enumerate(zip(chart.labels, chart.numbers(), strict=True)):
            u = chart.unit_at(i)
            tiles.append(f"{lab} {val}" + (f" {u}" if u and u != "%" else ""))
        parts = [f"Key figures: {chart.title}. " + "; ".join(tiles) + "."]
    else:
        bars = "; ".join(
            f"{lab} {val}" for lab, val in zip(chart.labels, chart.numbers(), strict=True)
        )
        unit = f" ({chart.unit})" if chart.unit and chart.unit != "%" else ""
        parts = [f"Bar chart: {chart.title}{unit}. {bars}."]
    if chart.note:
        parts.append(chart.note.rstrip(".") + ".")
    host = _HOST_RE.match(source_url or "")
    if host:
        parts.append(f"Source: {host.group(1)}.")
    text = " ".join(parts)
    return text if len(text) <= MAX_ALT_TEXT else text[: MAX_ALT_TEXT - 1] + "…"
