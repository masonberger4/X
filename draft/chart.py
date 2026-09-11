"""The draft's image: a chart or table the model SPECIFIES and code RENDERS.

The Anthropic API does not generate pictures, and a picture the pipeline cannot audit would
break "never fabricate numbers". So the drafter returns a small chart spec (title, labels,
values, unit) next to the post text; every number in it is checked against the source the
same way the post text is (draft.drafter.verify_chart), and a spec with an unverifiable
number is dropped, never rendered. render_chart() turns a verified spec into a PNG with
matplotlib (the `images` extra; imported inside the function so nothing else needs it).
The approval queue shows the PNG, a human can drop it, and step 3 attaches it to the first
post of whatever it publishes.

A Table is the second kind of visual: a small comparison (rows of entities, columns of
facts) such as a competitor landscape. Its cells go beyond the source, so unlike a chart it
is NOT rendered at draft time: step 2b (run_verify.py) fact-checks every cell on the web
first, blanks the ones it cannot support, drops the table when a cell is contradicted or
too few cells are supported, and only then renders it through render_table().
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

IMAGES_DIRNAME = "images"  # <db folder>/images/draft_<id>.png; shared by steps 2 and 3

CHART_MIN_BARS = 2
CHART_MAX_BARS = 8
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


class ChartError(ValueError):
    """The chart spec is malformed (structure), as opposed to unverifiable (numbers)."""


@dataclass
class Chart:
    title: str
    labels: list[str]
    values: list[float]
    unit: str = ""  # "%", "months", "" ... shown on the axis and after each value
    note: str = ""  # one line under the chart, e.g. "n=97, single arm, investigator-assessed"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def numbers(self) -> list[str]:
        """Every value as the text a reader sees, with its % sign, for the verbatim check."""
        return [format_value(v, self.unit) for v in self.values]

    def texts(self) -> list[str]:
        """The free-text parts (title, labels, note), whose numbers are checked too."""
        return [self.title, *self.labels, self.note]


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
        "note": {"type": "string", "description": "Optional footnote."},
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
    "required": ["title", "labels", "values", "unit"],
    "properties": {
        "title": {"type": "string", "description": "Chart title, one line."},
        "labels": {
            "type": "array",
            "minItems": CHART_MIN_BARS,
            "maxItems": CHART_MAX_BARS,
            "items": {"type": "string"},
            "description": "One label per bar, e.g. arm or endpoint names.",
        },
        "values": {
            "type": "array",
            "minItems": CHART_MIN_BARS,
            "maxItems": CHART_MAX_BARS,
            "items": {"type": "number"},
            "description": "One value per label, copied verbatim from the source.",
        },
        "unit": {"type": "string", "description": "'%', 'months', 'patients', or ''."},
        "note": {"type": "string", "description": "Optional footnote: n, design, caveat."},
    },
    "description": (
        "Optional bar chart to attach, or null when the source has no comparable numbers. "
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
    if len(labels) != len(values):
        raise ChartError("chart.labels and chart.values must have the same length")
    if not CHART_MIN_BARS <= len(labels) <= CHART_MAX_BARS:
        raise ChartError(f"chart needs {CHART_MIN_BARS}-{CHART_MAX_BARS} bars, got {len(labels)}")
    unit = data.get("unit", "")
    note = data.get("note", "")
    if not isinstance(unit, str) or not isinstance(note, str):
        raise ChartError("chart.unit and chart.note must be strings")
    return Chart(
        title=title.strip(),
        labels=[x.strip() for x in labels],
        values=[float(v) for v in values],
        unit=unit.strip(),
        note=note.strip(),
    )


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


def matplotlib_available() -> bool:
    try:
        import matplotlib  # noqa: F401
    except ImportError:
        return False
    return True


# --- rendering: one house style for every picture -----------------------------------------
# A 16:9 card: off-white surface, an accent rule and an eyebrow line at the top, a bold
# title, the data in one deep blue, a hairline footer with the source and note. Every
# number a reader sees is still the verified `format_value` text; the style changes nothing
# the fact checks look at.

SURFACE = "#fbfaf7"
INK = "#101418"
INK_2 = "#525a63"
INK_3 = "#8a929b"
RULE = "#dcdad3"
ACCENT = "#123f6b"  # the series colour; one hue, magnitude only
ACCENT_SOFT = "#c9d6e4"  # the track behind each bar
HEADER_FILL = "#123f6b"
ZEBRA = "#f1f0ec"
EYEBROW = "IMMUNO-ONCOLOGY  ·  DATA BRIEF"
FONT_FAMILIES = [
    "Inter",
    "Helvetica Neue",
    "Liberation Sans",
    "DejaVu Sans",
]  # first installed wins

# Figure coordinates (0-1) shared by charts and tables.
_MARGIN_X = 0.055
_TITLE_Y = 0.845
_FOOTER_Y = 0.055
_PLOT_TOP = 0.74
_PLOT_BOTTOM = 0.16


def _setup():
    """Import matplotlib for drawing (raises ImportError without it) and return pyplot."""
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib import font_manager

    installed = {f.name for f in font_manager.fontManager.ttflist}
    family = next((f for f in FONT_FAMILIES if f in installed), "sans-serif")
    plt.rcParams.update(
        {
            "font.family": family,
            "text.color": INK,
            "axes.edgecolor": RULE,
            "axes.labelcolor": INK_2,
            "xtick.color": INK_2,
            "ytick.color": INK_2,
            "svg.fonttype": "none",
        }
    )
    return plt


def _wrap(text: str, width: int) -> str:
    import textwrap

    return "\n".join(textwrap.wrap(text, width=width)) or text


def _frame(fig, plt, *, title: str, footer: list[str], subtitle: str = "") -> None:
    """The chrome around the data: accent rule, eyebrow, title, subtitle, footer rule
    and footer texts (note on the left, source on the right)."""
    from matplotlib.patches import Rectangle

    fig.patch.set_facecolor(SURFACE)
    # accent rule across the top
    fig.add_artist(Rectangle((0, 0.985), 1, 0.015, transform=fig.transFigure, color=ACCENT, lw=0))
    fig.text(
        _MARGIN_X,
        0.925,
        EYEBROW,
        fontsize=6.2,
        color=INK_3,
        fontweight="bold",
        ha="left",
        va="center",
    )
    fig.text(
        _MARGIN_X,
        _TITLE_Y,
        _wrap(title, 62),
        fontsize=12.5,
        fontweight="bold",
        color=INK,
        ha="left",
        va="center",
        linespacing=1.15,
    )
    if subtitle:
        fig.text(
            _MARGIN_X,
            _PLOT_TOP + 0.035,
            _wrap(subtitle, 120),
            fontsize=6.8,
            color=INK_2,
            ha="left",
            va="center",
        )
    fig.add_artist(
        plt.Line2D(
            [_MARGIN_X, 1 - _MARGIN_X],
            [_FOOTER_Y + 0.035] * 2,
            transform=fig.transFigure,
            color=RULE,
            lw=0.6,
        )
    )
    if footer:
        left, right = footer[0], footer[1:]
        fig.text(
            _MARGIN_X, _FOOTER_Y, _wrap(left, 110), fontsize=6, color=INK_2, ha="left", va="center"
        )
        if right:
            fig.text(
                1 - _MARGIN_X,
                _FOOTER_Y,
                "   ·   ".join(right),
                fontsize=6,
                color=INK_3,
                ha="right",
                va="center",
            )


def _source_label(source_url: str) -> str:
    host = _HOST_RE.match(source_url or "")
    return f"Source: {host.group(1)}" if host else ""


def render_chart(chart: Chart, path: str | Path, *, source_url: str = "") -> Path:
    """Draw the chart as a PNG at `path` (parent dirs created). Raises ImportError without
    matplotlib, which the callers turn into 'no image' rather than 'no draft'.

    Horizontal bars (arm and endpoint names are long), one hue, a light track showing the
    full scale behind each bar, the verified value at every bar's tip."""
    plt = _setup()
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fig = plt.figure(figsize=(WIDTH_PX / DPI, HEIGHT_PX / DPI), dpi=DPI)
    footer = [chart.note or " "]
    src = _source_label(source_url)
    if src:
        footer.append(src)
    unit_label = ""
    if chart.unit and chart.unit != "%":
        unit_label = f"Values in {chart.unit}"
    _frame(fig, plt, title=chart.title, footer=footer, subtitle=unit_label)

    n = len(chart.values)
    wrap_at = 30 if n <= 5 else 44  # dense charts get a wider gutter and fewer wrapped lines
    longest = max(len(line) for lab in chart.labels for line in _wrap(lab, wrap_at).split("\n"))
    label_w = min(max(0.05 + 0.0072 * longest, 0.12), 0.36)  # gutter for the bar labels
    avail = _PLOT_TOP - _PLOT_BOTTOM
    span = min(avail, 0.10 * n + 0.06)  # rows keep a fixed height, centred in the plot area
    bottom = _PLOT_BOTTOM + (avail - span) / 2
    ax = fig.add_axes((_MARGIN_X + label_w, bottom, 1 - 2 * _MARGIN_X - label_w - 0.06, span))
    ax.set_facecolor(SURFACE)
    top = max(chart.values + [0.0]) or 1.0
    scale = 100.0 if chart.unit.strip() == "%" and top <= 100 else top
    y = list(range(n))[::-1]  # first label at the top
    height = 0.46
    ax.barh(y, [scale] * n, height=height, color=ACCENT_SOFT, alpha=0.55, lw=0)
    ax.barh(y, chart.values, height=height, color=ACCENT, lw=0)
    ax.set_xlim(0, scale * 1.16)
    ax.set_ylim(-0.6, n - 0.4)
    ax.set_yticks(y)
    ax.set_yticklabels(
        [_wrap(lab, wrap_at) for lab in chart.labels],
        fontsize=7.2 if n <= 5 else 6.4,
        color=INK,
        linespacing=1.1,
    )
    ax.tick_params(axis="y", length=0, pad=10)
    ax.tick_params(axis="x", labelsize=6, length=0, colors=INK_3)
    ax.xaxis.grid(True, color=RULE, lw=0.5)
    ax.set_axisbelow(True)
    ticks = ax.get_xticks()
    ax.set_xticks([t for t in ticks if 0 <= t <= scale])
    if chart.unit.strip() == "%":
        ax.set_xticklabels([f"{t:g}%" for t in ax.get_xticks()])
    for side in ax.spines.values():
        side.set_visible(False)
    for yi, val, text in zip(y, chart.values, chart.numbers(), strict=True):
        ax.annotate(
            text,
            (val, yi),
            xytext=(5, 0),
            textcoords="offset points",
            ha="left",
            va="center",
            fontsize=8,
            fontweight="bold",
            color=INK,
        )
    fig.savefig(path, format="png", dpi=DPI, facecolor=SURFACE)
    plt.close(fig)
    return path


def render_table(
    table: Table,
    path: str | Path,
    *,
    source_url: str = "",
    blanked: frozenset[tuple[int, int]] = frozenset(),
) -> Path:
    """Draw the table as a PNG at `path`. Cells in `blanked` (the fact-checker could not
    support them) are drawn as BLANK_CELL and the footer says so. Raises ImportError without
    matplotlib, like render_chart.

    A navy header row, zebra rows, horizontal hairlines only, bold row labels."""
    plt = _setup()
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fig = plt.figure(figsize=(WIDTH_PX / DPI, HEIGHT_PX / DPI), dpi=DPI)
    footer = [table.note or " "]
    if blanked:
        footer.append(f"{BLANK_CELL} not verifiable against a primary source")
    src = _source_label(source_url)
    if src:
        footer.append(src)
    _frame(fig, plt, title=table.title, footer=footer)

    body = [
        [BLANK_CELL if (r, c) in blanked or not cell else cell for c, cell in enumerate(row)]
        for r, row in enumerate(table.rows)
    ]
    # Column widths follow the longest text in each column (headers included).
    longest = [max(len(col), *(len(row[c]) for row in body)) for c, col in enumerate(table.columns)]
    longest = [min(max(n, 6), 40) for n in longest]
    widths = [n / sum(longest) for n in longest]
    nrows = len(body) + 1
    row_h = min(0.105, (_PLOT_TOP + 0.03 - _PLOT_BOTTOM) / nrows)
    ax = fig.add_axes((_MARGIN_X, _PLOT_BOTTOM, 1 - 2 * _MARGIN_X, _PLOT_TOP + 0.03 - _PLOT_BOTTOM))
    ax.axis("off")
    tbl = ax.table(
        cellText=[[_wrap(c, 34) for c in row] for row in body],
        colLabels=[c.upper() for c in table.columns],
        colWidths=widths,
        cellLoc="left",
        colLoc="left",
        loc="upper left",
        bbox=(
            0.0,
            1 - row_h * nrows / (_PLOT_TOP + 0.03 - _PLOT_BOTTOM),
            1.0,
            row_h * nrows / (_PLOT_TOP + 0.03 - _PLOT_BOTTOM),
        ),
    )
    tbl.auto_set_font_size(False)
    tbl.set_fontsize(6.8)
    for (r, c), cell in tbl.get_celld().items():
        cell.PAD = 0.05
        cell.set_linewidth(0.5)
        if r == 0:
            cell.set_facecolor(HEADER_FILL)
            cell.set_edgecolor(HEADER_FILL)
            cell.set_text_props(fontweight="bold", color="white", fontsize=5.8)
            continue
        cell.visible_edges = "B"
        cell.set_edgecolor(RULE)
        cell.set_facecolor(ZEBRA if r % 2 == 0 else SURFACE)
        if c == 0:
            cell.set_text_props(fontweight="bold", color=INK)
        else:
            cell.set_text_props(color=INK)
        if (r - 1, c) in blanked:
            cell.set_text_props(color=INK_3)
    fig.savefig(path, format="png", dpi=DPI, facecolor=SURFACE)
    plt.close(fig)
    return path


__all__ = [
    "BLANK_CELL",
    "CHART_JSON_SCHEMA",
    "TABLE_JSON_SCHEMA",
    "Table",
    "render_table",
    "table_from_json",
    "validate_table",
    "visual_from_json",
    "Chart",
    "ChartError",
    "alt_text",
    "chart_from_json",
    "format_value",
    "matplotlib_available",
    "render_chart",
    "validate_chart",
]
