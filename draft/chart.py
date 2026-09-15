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


# --- rendering: one card layout, many palettes ----------------------------------------------
# A 16:9 card: a surface, an accent rule and an eyebrow line at the top, a bold title, the
# data in the palette's series colour, a hairline footer with the source and note. Every
# number a reader sees is still the verified `format_value` text; the style changes nothing
# the fact checks look at. Which palette a card uses is a Style knob (`Style.palette`), so a
# designer genome and the image grader can both choose it: the layout is the house style,
# the colours are meant to vary from card to card.


@dataclass(frozen=True)
class Palette:
    """The colours of one card. `series` is the categorical run used when
    `Style.multi_colour` is on (one hue per bar); `accent` is the single series colour,
    `tint` the comparators when the first bar is highlighted, `soft` the track."""

    surface: str
    ink: str
    ink_2: str
    ink_3: str
    rule: str
    accent: str
    soft: str
    tint: str
    zebra: str
    series: tuple[str, ...]
    header: str = ""  # table header fill; the accent when empty
    shadow: str = "#08213d"

    @property
    def header_fill(self) -> str:
        return self.header or self.accent


_LIGHT = dict(surface="#fbfaf7", ink="#101418", ink_2="#525a63", ink_3="#8a929b", rule="#dcdad3")
_DARK = dict(surface="#141a22", ink="#f2f4f6", ink_2="#b7bec7", ink_3="#7d8791", rule="#2c3540")
_SERIES = ("#123f6b", "#c2410c", "#0f766e", "#6d28d9", "#b45309", "#be123c", "#3f6212", "#0e7490")

PALETTES: dict[str, Palette] = {
    "navy": Palette(
        **_LIGHT, accent="#123f6b", soft="#c9d6e4", tint="#7f9bbd", zebra="#e9edf2", series=_SERIES
    ),
    "teal": Palette(
        **_LIGHT,
        accent="#0f766e",
        soft="#c6e4df",
        tint="#7cbcb4",
        zebra="#e6f1ef",
        series=(
            "#0f766e",
            "#c2410c",
            "#123f6b",
            "#6d28d9",
            "#b45309",
            "#be123c",
            "#3f6212",
            "#0e7490",
        ),
        shadow="#093f3a",
    ),
    "crimson": Palette(
        **_LIGHT,
        accent="#9f1239",
        soft="#f0d3db",
        tint="#d07d94",
        zebra="#f6eaee",
        series=(
            "#9f1239",
            "#123f6b",
            "#0f766e",
            "#b45309",
            "#6d28d9",
            "#3f6212",
            "#0e7490",
            "#c2410c",
        ),
        shadow="#4c0519",
    ),
    "forest": Palette(
        **_LIGHT,
        accent="#166534",
        soft="#cfe4d6",
        tint="#7fb894",
        zebra="#e8f1eb",
        series=(
            "#166534",
            "#c2410c",
            "#123f6b",
            "#b45309",
            "#6d28d9",
            "#be123c",
            "#0e7490",
            "#3f6212",
        ),
        shadow="#052e16",
    ),
    "amber": Palette(
        **_LIGHT,
        accent="#b45309",
        soft="#f3e1c8",
        tint="#dda36b",
        zebra="#f7efe4",
        series=(
            "#b45309",
            "#123f6b",
            "#0f766e",
            "#9f1239",
            "#6d28d9",
            "#3f6212",
            "#0e7490",
            "#c2410c",
        ),
        shadow="#451a03",
    ),
    "plum": Palette(
        **_LIGHT,
        accent="#6d28d9",
        soft="#e0d6f5",
        tint="#ac8fe6",
        zebra="#efeaf8",
        series=(
            "#6d28d9",
            "#0f766e",
            "#c2410c",
            "#123f6b",
            "#b45309",
            "#be123c",
            "#3f6212",
            "#0e7490",
        ),
        shadow="#2e1065",
    ),
    "slate": Palette(
        **_LIGHT,
        accent="#334155",
        soft="#d9dee6",
        tint="#8e9bad",
        zebra="#eaedf1",
        series=(
            "#334155",
            "#c2410c",
            "#0f766e",
            "#6d28d9",
            "#b45309",
            "#be123c",
            "#123f6b",
            "#0e7490",
        ),
        shadow="#0f172a",
    ),
    "midnight": Palette(  # the one dark card: pale ink on a near-black surface
        **_DARK,
        accent="#60a5fa",
        soft="#243244",
        tint="#3b6ea5",
        zebra="#1b232e",
        series=(
            "#60a5fa",
            "#fb923c",
            "#2dd4bf",
            "#c084fc",
            "#fbbf24",
            "#fb7185",
            "#a3e635",
            "#22d3ee",
        ),
        header="#1d4ed8",
        shadow="#000000",
    ),
}
DEFAULT_PALETTE = "navy"

EYEBROW = "IMMUNO-ONCOLOGY  ·  DATA BRIEF"
FONT_FAMILIES = [
    "Inter",
    "Helvetica Neue",
    "Liberation Sans",
    "DejaVu Sans",
]  # first installed wins


@dataclass
class Style:
    """Layout knobs the image grader (draft/grader.py) may turn between iterations. Each is
    clamped to its range by `clamp`, so a grader can never push text off the card. Defaults
    are the house style; None from the grader means "leave as is"."""

    font_scale: float = 1.0  # every text size except the title (0.7-1.6)
    title_scale: float = 1.0  # the title alone (0.7-1.6)
    bar_height: float = 0.46  # bar thickness as a fraction of the row pitch (0.3-0.8)
    row_pitch: float = 0.10  # figure fraction per chart row (0.06-0.16)
    label_wrap: int = 30  # characters per line for bar labels (16-60)
    highlight_first: bool = False  # first bar in navy, the rest in a tint
    gridlines: bool = True
    track: bool = True  # light bar behind each bar showing the full scale
    table_row_height: float = 0.105  # figure fraction per table row (0.06-0.16)
    palette: str = DEFAULT_PALETTE  # one of PALETTES: the card's colours
    multi_colour: bool = False  # one hue per bar from the palette's series (chart only)

    RANGES = {
        "font_scale": (0.7, 1.6),
        "title_scale": (0.7, 1.6),
        "bar_height": (0.3, 0.8),
        "row_pitch": (0.06, 0.16),
        "label_wrap": (16, 60),
        "table_row_height": (0.06, 0.16),
    }
    CHOICES = {"palette": tuple(PALETTES)}

    @property
    def colours(self) -> Palette:
        return PALETTES.get(self.palette, PALETTES[DEFAULT_PALETTE])

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def apply(self, changes: dict[str, Any] | None) -> Style:
        """A copy with `changes` applied: unknown keys and None values are ignored, numbers
        are clamped to their range, a choice outside its options is ignored, flags are
        coerced to bool."""
        from dataclasses import replace

        clean: dict[str, Any] = {}
        for key, value in (changes or {}).items():
            if value is None or key not in self.__dataclass_fields__:
                continue
            if key in self.RANGES:
                lo, hi = self.RANGES[key]
                try:
                    num = float(value)
                except (TypeError, ValueError):
                    continue
                num = min(max(num, lo), hi)
                clean[key] = int(round(num)) if key == "label_wrap" else num
            elif key in self.CHOICES:
                name = str(value).strip().lower()
                if name in self.CHOICES[key]:
                    clean[key] = name
            else:
                clean[key] = bool(value)
        return replace(self, **clean)


# Figure coordinates (0-1) shared by charts and tables.
_MARGIN_X = 0.055
_TITLE_Y = 0.845
_FOOTER_Y = 0.055
_PLOT_TOP = 0.74
_PLOT_BOTTOM = 0.16


def _setup(pal: Palette | None = None):
    """Import matplotlib for drawing (raises ImportError without it) and return pyplot,
    with the text and axis colours of `pal` (the house palette by default)."""
    pal = pal or PALETTES[DEFAULT_PALETTE]
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib import font_manager

    installed = {f.name for f in font_manager.fontManager.ttflist}
    family = next((f for f in FONT_FAMILIES if f in installed), "sans-serif")
    plt.rcParams.update(
        {
            "font.family": family,
            "text.color": pal.ink,
            "axes.edgecolor": pal.rule,
            "axes.labelcolor": pal.ink_2,
            "xtick.color": pal.ink_2,
            "ytick.color": pal.ink_2,
            "svg.fonttype": "none",
        }
    )
    return plt


def _wrap(text: str, width: int) -> str:
    import textwrap

    return "\n".join(textwrap.wrap(text, width=width)) or text


def _frame(
    fig, plt, *, title: str, footer: list[str], subtitle: str = "", style: Style | None = None
) -> None:
    """The chrome around the data: accent rule, eyebrow, title, subtitle, footer rule
    and footer texts (note on the left, source on the right)."""
    from matplotlib.patches import Rectangle

    st = style or Style()
    pal = st.colours
    fs, ts = st.font_scale, st.title_scale
    fig.patch.set_facecolor(pal.surface)
    # accent rule across the top
    fig.add_artist(
        Rectangle((0, 0.985), 1, 0.015, transform=fig.transFigure, color=pal.accent, lw=0)
    )
    fig.text(
        _MARGIN_X,
        0.925,
        EYEBROW,
        fontsize=6.2 * fs,
        color=pal.ink_3,
        fontweight="bold",
        ha="left",
        va="center",
    )
    fig.text(
        _MARGIN_X,
        _TITLE_Y,
        _wrap(title, max(20, int(62 / ts))),
        fontsize=12.5 * ts,
        fontweight="bold",
        color=pal.ink,
        ha="left",
        va="center",
        linespacing=1.15,
    )
    if subtitle:
        fig.text(
            _MARGIN_X,
            _PLOT_TOP + 0.035,
            _wrap(subtitle, 120),
            fontsize=6.8 * fs,
            color=pal.ink_2,
            ha="left",
            va="center",
        )
    fig.add_artist(
        plt.Line2D(
            [_MARGIN_X, 1 - _MARGIN_X],
            [_FOOTER_Y + 0.035] * 2,
            transform=fig.transFigure,
            color=pal.rule,
            lw=0.6,
        )
    )
    if footer:
        left, right = footer[0], footer[1:]
        fig.text(
            _MARGIN_X,
            _FOOTER_Y,
            _wrap(left, 110),
            fontsize=6 * fs,
            color=pal.ink_2,
            ha="left",
            va="center",
        )
        if right:
            fig.text(
                1 - _MARGIN_X,
                _FOOTER_Y,
                "   ·   ".join(right),
                fontsize=6 * fs,
                color=pal.ink_3,
                ha="right",
                va="center",
            )


def _source_label(source_url: str) -> str:
    host = _HOST_RE.match(source_url or "")
    return f"Source: {host.group(1)}" if host else ""


def bar_colours(style: Style, n: int) -> list[str]:
    """The fill of each of `n` bars: one hue per bar when `multi_colour` is on, the accent
    then a tint when `highlight_first` is on, else the accent throughout."""
    pal = style.colours
    if style.multi_colour:
        return [pal.series[i % len(pal.series)] for i in range(n)]
    if style.highlight_first:
        return [pal.accent] + [pal.tint] * (n - 1)
    return [pal.accent] * n


_CHART_LOGO_W = 0.05  # figure fraction added to the label gutter when a bar carries a logo


def _draw_chart_logo(fig, ax, yi: float, logo: Path) -> None:
    """A logo PNG at the left edge of the card, on the bar's row, scaled to ~70% of the
    row pitch. An unreadable file is skipped: the ticker in the label still carries it."""
    from matplotlib.image import imread
    from matplotlib.offsetbox import AnnotationBbox, OffsetImage

    try:
        img = imread(str(logo))
    except Exception:
        return
    ax_h_px = ax.get_window_extent().height
    rows = max(ax.get_ylim()[1] - ax.get_ylim()[0], 1.0)
    target_px = ax_h_px / rows * 0.7
    zoom = target_px / max(img.shape[0], 1) * (72.0 / fig.dpi)
    box = AnnotationBbox(
        OffsetImage(img, zoom=zoom),
        (_MARGIN_X + _CHART_LOGO_W / 2, yi),
        xycoords=("figure fraction", "data"),
        frameon=False,
        box_alignment=(0.5, 0.5),
        zorder=6,
    )
    ax.add_artist(box)


def render_chart(
    chart: Chart,
    path: str | Path,
    *,
    source_url: str = "",
    style: Style | None = None,
    logos: dict[int, Path] | None = None,
) -> Path:
    """Draw the chart as a PNG at `path` (parent dirs created). Raises ImportError without
    matplotlib, which the callers turn into 'no image' rather than 'no draft'.

    Horizontal bars (arm and endpoint names are long), the palette's series colour (or one
    hue per bar with `multi_colour`), a light track showing the full scale behind each bar,
    the verified value at every bar's tip. `logos` maps a bar index to a PNG drawn in the
    label gutter (draft/branding.py:brand_chart)."""
    st = style or Style()
    pal = st.colours
    fs = st.font_scale
    logos = logos or {}
    plt = _setup(pal)
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
    _frame(fig, plt, title=chart.title, footer=footer, subtitle=unit_label, style=st)

    n = len(chart.values)
    wrap_at = st.label_wrap if n <= 5 else max(st.label_wrap, 44)  # dense charts wrap less
    longest = max(len(line) for lab in chart.labels for line in _wrap(lab, wrap_at).split("\n"))
    label_w = min(max(0.05 + 0.0072 * longest * fs, 0.12), 0.36)  # gutter for the bar labels
    if logos:
        label_w = min(label_w + _CHART_LOGO_W, 0.42)
    avail = _PLOT_TOP - _PLOT_BOTTOM
    span = min(avail, st.row_pitch * n + 0.06)  # rows keep a fixed height, centred
    bottom = _PLOT_BOTTOM + (avail - span) / 2
    ax = fig.add_axes((_MARGIN_X + label_w, bottom, 1 - 2 * _MARGIN_X - label_w - 0.06, span))
    ax.set_facecolor(pal.surface)
    top = max(chart.values + [0.0]) or 1.0
    scale = 100.0 if chart.unit.strip() == "%" and top <= 100 else top
    y = list(range(n))[::-1]  # first label at the top
    height = st.bar_height
    if st.track:
        ax.barh(y, [scale] * n, height=height, color=pal.soft, alpha=0.55, lw=0)
    ax.barh(y, chart.values, height=height, color=bar_colours(st, n), lw=0)
    ax.set_xlim(0, scale * 1.16)
    ax.set_ylim(-0.6, n - 0.4)
    ax.set_yticks(y)
    ax.set_yticklabels(
        [_wrap(lab, wrap_at) for lab in chart.labels],
        fontsize=7.2 if n <= 5 else 6.4,
        color=pal.ink,
        linespacing=1.1,
    )
    ax.tick_params(axis="y", length=0, pad=10)
    ax.tick_params(axis="x", labelsize=6 * fs, length=0, colors=pal.ink_3)
    if st.gridlines:
        ax.xaxis.grid(True, color=pal.rule, lw=0.5)
    else:
        ax.xaxis.grid(False)
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
            fontsize=8 * fs,
            fontweight="bold",
            color=pal.ink,
        )
    for i, logo in logos.items():
        if 0 <= i < n:
            _draw_chart_logo(fig, ax, y[i], logo)
    fig.savefig(path, format="png", dpi=DPI, facecolor=pal.surface)
    plt.close(fig)
    return path


def render_table(
    table: Table,
    path: str | Path,
    *,
    source_url: str = "",
    blanked: frozenset[tuple[int, int]] = frozenset(),
    style: Style | None = None,
    logos: dict[tuple[int, int], Path] | None = None,
) -> Path:
    """Draw the table as a PNG at `path`. Cells in `blanked` (the fact-checker could not
    support them) are drawn as BLANK_CELL and the footer says so. `logos` maps a body cell
    (row, col) to a PNG drawn at the cell's left edge (draft/branding.py). Raises
    ImportError without matplotlib, like render_chart.

    A rounded header bar in the palette's header colour with a drop shadow and a top
    sheen, zebra rows, horizontal hairlines only, bold row labels."""
    st = style or Style()
    pal = st.colours
    fs = st.font_scale
    plt = _setup(pal)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fig = plt.figure(figsize=(WIDTH_PX / DPI, HEIGHT_PX / DPI), dpi=DPI)
    footer = [table.note or " "]
    if blanked:
        footer.append(f"{BLANK_CELL} not verifiable against a primary source")
    src = _source_label(source_url)
    if src:
        footer.append(src)
    _frame(fig, plt, title=table.title, footer=footer, style=st)

    body = [
        [BLANK_CELL if (r, c) in blanked or not cell else cell for c, cell in enumerate(row)]
        for r, row in enumerate(table.rows)
    ]
    # Column widths follow the longest text in each column (headers included).
    longest = [max(len(col), *(len(row[c]) for row in body)) for c, col in enumerate(table.columns)]
    longest = [min(max(n, 6), 40) for n in longest]
    widths = [n / sum(longest) for n in longest]
    nrows = len(body) + 1
    row_h = min(st.table_row_height, (_PLOT_TOP + 0.03 - _PLOT_BOTTOM) / nrows)
    ax = fig.add_axes((_MARGIN_X, _PLOT_BOTTOM, 1 - 2 * _MARGIN_X, _PLOT_TOP + 0.03 - _PLOT_BOTTOM))
    ax.axis("off")
    tbl = ax.table(
        cellText=[[_wrap(c, max(12, int(34 / fs))) for c in row] for row in body],
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
    tbl.set_fontsize(6.8 * fs)
    logos = logos or {}
    for (r, c), cell in tbl.get_celld().items():
        cell.PAD = 0.05
        cell.set_linewidth(0.5)
        if r == 0:
            cell.set_visible(False)  # drawn as one rounded bar by _header_bar below
            continue
        if (r - 1, c) in logos:
            cell.PAD = 0.05 + _LOGO_PAD / max(widths[c], 0.01)
        cell.set_linewidth(0)  # rows are shaded and ruled by _row_bands below
        cell.set_facecolor(pal.zebra if r % 2 == 0 else pal.surface)
        if c == 0:
            cell.set_text_props(fontweight="bold", color=pal.ink)
        else:
            cell.set_text_props(color=pal.ink)
        if (r - 1, c) in blanked:
            cell.set_text_props(color=pal.ink_3)
    fig.canvas.draw()  # positions the cells so the header bar and logos can use them
    _row_bands(ax, tbl, len(body), len(table.columns), pal)
    _header_bar(ax, tbl, [c.upper() for c in table.columns], fontsize=5.8 * fs, pal=pal)
    for (r, c), logo in logos.items():
        _draw_logo(ax, tbl, r + 1, c, logo)
    fig.savefig(path, format="png", dpi=DPI, facecolor=pal.surface)
    plt.close(fig)
    return path


_LOGO_PAD = 0.055  # axes fraction reserved left of the text in a cell that carries a logo
HEADER_SHADOW = PALETTES[DEFAULT_PALETTE].shadow
HEADER_RADIUS = 0.012  # axes fraction


def _row_bands(ax, tbl, nrows: int, ncols: int, pal: Palette | None = None) -> None:
    """Zebra shading and one hairline under each body row, drawn as plain patches: a
    matplotlib table cell only paints along its visible edges, so a cell with just a
    bottom edge shows no fill."""
    from matplotlib.patches import Rectangle

    pal = pal or PALETTES[DEFAULT_PALETTE]
    for r in range(1, nrows + 1):
        cells = [tbl[r, c] for c in range(ncols)]
        x0 = min(c.get_x() for c in cells)
        x1 = max(c.get_x() + c.get_width() for c in cells)
        y0, h = cells[0].get_y(), cells[0].get_height()
        if r % 2 == 0:
            ax.add_patch(
                Rectangle(
                    (x0, y0),
                    x1 - x0,
                    h,
                    facecolor=pal.zebra,
                    lw=0,
                    zorder=1,
                    transform=ax.transAxes,
                )
            )
        ax.plot([x0, x1], [y0, y0], color=pal.rule, lw=0.5, zorder=1.5, transform=ax.transAxes)
    tbl.set_zorder(2)  # the table (and its text) above the bands, below the header bar
    for cell in tbl.get_celld().values():
        cell.set_facecolor("none")


def _header_bar(ax, tbl, labels: list[str], *, fontsize: float, pal: Palette | None = None) -> None:
    """One rounded bar over the header row: a soft drop shadow beneath, the bar, a
    translucent sheen on its upper half (the 3D read), then the column labels."""
    from matplotlib.patches import FancyBboxPatch, Rectangle

    pal = pal or PALETTES[DEFAULT_PALETTE]

    cells = [tbl[0, c] for c in range(len(labels))]
    x0 = min(c.get_x() for c in cells)
    x1 = max(c.get_x() + c.get_width() for c in cells)
    y0 = min(c.get_y() for c in cells)
    h = max(c.get_height() for c in cells)
    box = dict(boxstyle=f"round,pad=0,rounding_size={HEADER_RADIUS}", lw=0)
    ax.add_patch(
        FancyBboxPatch(
            (x0 + 0.004, y0 - 0.012),
            x1 - x0,
            h,
            facecolor=pal.shadow,
            alpha=0.28,
            zorder=3,
            transform=ax.transAxes,
            **box,
        )
    )
    bar = FancyBboxPatch(
        (x0, y0), x1 - x0, h, facecolor=pal.header_fill, zorder=4, transform=ax.transAxes, **box
    )
    ax.add_patch(bar)
    sheen = Rectangle(
        (x0, y0 + h * 0.5),
        x1 - x0,
        h * 0.5,
        facecolor="white",
        alpha=0.10,
        lw=0,
        zorder=5,
        transform=ax.transAxes,
    )
    sheen.set_clip_path(bar)
    ax.add_patch(sheen)
    for cell, label in zip(cells, labels, strict=True):
        ax.text(
            cell.get_x() + cell.get_width() * cell.PAD,
            y0 + h / 2,
            label,
            fontsize=fontsize,
            fontweight="bold",
            color="white",
            ha="left",
            va="center",
            zorder=6,
            transform=ax.transAxes,
        )


def _draw_logo(ax, tbl, row: int, col: int, logo: Path) -> None:
    """A logo PNG at the left edge of a body cell, scaled to ~60% of the row height.
    An unreadable file is skipped: the ticker text still carries the identity."""
    from matplotlib.image import imread
    from matplotlib.offsetbox import AnnotationBbox, OffsetImage

    try:
        img = imread(str(logo))
    except Exception:
        return
    cell = tbl[row, col]
    fig = ax.figure
    ax_h_px = ax.get_window_extent().height
    target_px = cell.get_height() * ax_h_px * 0.6
    zoom = target_px / max(img.shape[0], 1) * (72.0 / fig.dpi)
    box = AnnotationBbox(
        OffsetImage(img, zoom=zoom),
        (cell.get_x() + _LOGO_PAD / 2 + 0.006, cell.get_y() + cell.get_height() / 2),
        xycoords=ax.transAxes,
        frameon=False,
        box_alignment=(0.5, 0.5),
        zorder=6,
    )
    ax.add_artist(box)


__all__ = [
    "BLANK_CELL",
    "CHART_JSON_SCHEMA",
    "DEFAULT_PALETTE",
    "PALETTES",
    "Palette",
    "TABLE_JSON_SCHEMA",
    "Style",
    "bar_colours",
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
