"""The draft's image: a chart the model SPECIFIES and code RENDERS.

The Anthropic API does not generate pictures, and a picture the pipeline cannot audit would
break "never fabricate numbers". So the drafter returns a small chart spec (title, labels,
values, unit) next to the post text; every number in it is checked against the source the
same way the post text is (draft.drafter.verify_chart), and a spec with an unverifiable
number is dropped, never rendered. render_chart() turns a verified spec into a PNG with
matplotlib (the `images` extra; imported inside the function so nothing else needs it).
The approval queue shows the PNG, a human can drop it, and step 3 attaches it to the first
post of whatever it publishes.
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


def chart_from_json(text: str | None) -> Chart | None:
    """A Chart from the JSON stored in drafts.chart_json; None when empty or unreadable."""
    import json

    if not text:
        return None
    try:
        return validate_chart(json.loads(text))
    except (ValueError, TypeError):
        return None


def alt_text(chart: Chart, source_url: str = "") -> str:
    """Alt text for the image: the title and every bar, so the chart reads without the picture."""
    bars = "; ".join(f"{lab} {val}" for lab, val in zip(chart.labels, chart.numbers(), strict=True))
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


def render_chart(chart: Chart, path: str | Path, *, source_url: str = "") -> Path:
    """Draw the chart as a PNG at `path` (parent dirs created). Raises ImportError without
    matplotlib, which the callers turn into 'no image' rather than 'no draft'."""
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fig, ax = plt.subplots(figsize=(WIDTH_PX / DPI, HEIGHT_PX / DPI), dpi=DPI)
    fig.patch.set_facecolor("white")
    x = range(len(chart.values))
    bars = ax.bar(x, chart.values, color="#1f4e79", width=0.6)
    ax.set_xticks(list(x))
    ax.set_xticklabels(chart.labels, fontsize=7, wrap=True)
    ax.set_title(chart.title, fontsize=9, fontweight="bold", loc="left", pad=10)
    if chart.unit:
        ax.set_ylabel(chart.unit, fontsize=7)
    ax.tick_params(axis="y", labelsize=7)
    for bar, text in zip(bars, chart.numbers(), strict=True):
        ax.annotate(
            text,
            (bar.get_x() + bar.get_width() / 2, bar.get_height()),
            ha="center",
            va="bottom",
            fontsize=7,
            xytext=(0, 2),
            textcoords="offset points",
        )
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    ax.set_ylim(0, max(chart.values + [0.0]) * 1.18 or 1)
    footer = []
    if chart.note:
        footer.append(chart.note)
    host = _HOST_RE.match(source_url or "")
    if host:
        footer.append(f"Source: {host.group(1)}")
    if footer:
        fig.text(0.02, 0.02, "  ·  ".join(footer), fontsize=6, color="#555555")
    fig.tight_layout(rect=(0, 0.06 if footer else 0, 1, 1))
    fig.savefig(path, format="png", dpi=DPI, facecolor="white")
    plt.close(fig)
    return path


__all__ = [
    "CHART_JSON_SCHEMA",
    "Chart",
    "ChartError",
    "alt_text",
    "chart_from_json",
    "format_value",
    "matplotlib_available",
    "render_chart",
    "validate_chart",
]
