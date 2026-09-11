"""The image grader: a second model looks at the rendered PNG and scores it.

After `approval_queue/images.py` draws a chart or table it shows the picture to the grader,
which answers with a 1-10 score against three criteria (easy to read, uses colour and
graphics well, limits negative space), the flaws it sees, actionable fixes, and a set of
layout knob changes (`draft.chart.Style`) for the next attempt. A picture at or above
`images.grader.min_score` is done; below it the renderer applies the knob changes and
draws again, up to `images.grader.max_iterations` renders in total. The best-scoring
render is what the draft keeps. Every grade is stored in `image_grades`
(approval_queue/store.py) so the queue can show it.

The grader only ever changes LAYOUT. It cannot add, remove or edit a number, a label or a
title: those come from the verified spec, and the knobs it may turn are clamped by
`Style.apply`. `call_grader` is this module's single network call (the Anthropic API with
an image block, or the Claude Code CLI reading the file with its Read tool when
`models.backend: claude_code`). Settings: `images.grader` in draft/config.yaml.
"""

from __future__ import annotations

import base64
import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import claude_cli
from draft.chart import Chart, Style, Table
from draft.drafter import parse_json_response
from draft.settings import load_draft_config

log = logging.getLogger(__name__)

MAX_TOKENS = 1500
DEFAULT_MIN_SCORE = 8
DEFAULT_MAX_ITERATIONS = 4
MAX_TEXT_ITEMS = 8  # flaws / fixes kept per grade
MAX_TEXT_CHARS = 300

CRITERIA = (
    "the image is easy to read",
    "it uses colour and graphics well",
    "it limits negative space (empty areas)",
)

# The professional-finish checklist the grader scores 1-10 each, alongside the three
# headline criteria. Names are the keys of ImageGrade.criteria.
CHECKLIST: dict[str, str] = {
    "readability": "easy to read at timeline size (a 600 px wide thumbnail): text large "
    "enough, strong contrast, nothing clipped or overlapping",
    "colour_graphics": "colour and graphics used well: one accent hue, neutral ink, "
    "recessive rules, emphasis on the key row or bar",
    "negative_space": "limited negative space: the card is filled, no empty band larger than a row",
    "hierarchy": "typographic hierarchy: eyebrow, title, headers, body and footer each "
    "clearly one level, title dominant",
    "alignment": "alignment and grid: columns and labels share a left edge, consistent "
    "margins and gutters, the header bar spans the table exactly",
    "header_finish": "header boxes have rounded corners and a 3D finish (drop shadow and "
    "top sheen), crisp and even",
    "branding_cells": "company or sponsor cells show the company's logo and stock ticker "
    "where available; logos are sized to the row and aligned with the text",
    "number_format": "numbers and units are consistently formatted (same decimals, unit "
    "shown once, thousands separated) and value labels sit clear of the bar ends",
    "source_footer": "the source and note are legible but recessive, on one footer line",
    "consistency": "consistent with the house style: same eyebrow, accent colour, fonts "
    "and layout as other cards from this account",
}


class GraderError(RuntimeError):
    """The grader's reply could not be used (bad JSON, missing score)."""


@dataclass
class ImageGrade:
    score: int
    criteria: dict[str, int] = field(default_factory=dict)  # CHECKLIST name -> 1-10
    flaws: list[str] = field(default_factory=list)
    fixes: list[str] = field(default_factory=list)
    adjustments: dict[str, Any] = field(default_factory=dict)  # Style changes, already clamped
    model: str = ""
    iteration: int = 1
    raw: str = ""

    def passed(self, min_score: int) -> bool:
        return self.score >= min_score


@dataclass
class GraderSettings:
    enabled: bool = True
    model: str = ""
    min_score: int = DEFAULT_MIN_SCORE
    max_iterations: int = DEFAULT_MAX_ITERATIONS


def grader_settings(cfg: dict | None = None) -> GraderSettings:
    """`images.grader` from draft/config.yaml. The model: IMAGE_GRADER_MODEL env, then root
    config.yaml `models.image_grader` if present, then the draft config value."""
    cfg = cfg if cfg is not None else load_draft_config()
    section = (cfg.get("images") or {}).get("grader") or {}
    model = os.environ.get("IMAGE_GRADER_MODEL", "").strip()
    if not model:
        try:
            import config as root_config

            model = str((root_config.load_config().get("models") or {}).get("image_grader") or "")
        except Exception:  # root config.yaml missing or unreadable
            model = ""
    if not model:
        model = str(section.get("model") or "")
    return GraderSettings(
        enabled=bool(section.get("enabled", True)) and bool(model),
        model=model,
        min_score=int(section.get("min_score", DEFAULT_MIN_SCORE)),
        max_iterations=max(1, int(section.get("max_iterations", DEFAULT_MAX_ITERATIONS))),
    )


_CHECKLIST_TEXT = "\n".join(f"- {name}: {text}" for name, text in CHECKLIST.items())

SYSTEM_PROMPT = f"""You are a strict graphic-design grader for data graphics attached to X posts
by a biotech investing account. You are shown one rendered PNG (1600x900) and its spec.

Grade the picture 1-10 overall against three headline criteria: (1) {CRITERIA[0]},
(2) {CRITERIA[1]}, (3) {CRITERIA[2]}. Then score each item of this professional-finish
checklist 1-10 (10 = nothing to improve):
{_CHECKLIST_TEXT}

The overall score is the picture as a whole, not an average: one glaring flaw caps it.
Be honest and specific: list every flaw you see and where it is, and for each flaw a
concrete fix. Never comment on the data, and never propose adding, removing or changing
any number, label, title, ticker or note: every word in the picture is fact-checked or
comes from config, and the renderer will not change it. A logo or ticker that is absent
is absent because the company is not configured, so do not ask for one to be invented;
score branding_cells on what is there. Do not suggest logos or brand names for the
account itself.

You steer the next render only through these layout knobs (omit any you would keep):
- font_scale (0.7-1.6): every text size except the title
- title_scale (0.7-1.6): the title
- bar_height (0.3-0.8): bar thickness as a fraction of the row pitch (chart only)
- row_pitch (0.06-0.16): vertical space per bar; larger fills the card (chart only)
- label_wrap (16-60): characters per line before a bar label wraps (chart only)
- highlight_first (true/false): first bar in navy, the rest in a lighter tint
- gridlines (true/false)
- track (true/false): the light bar behind each bar showing the full scale
- table_row_height (0.06-0.16): vertical space per table row (table only)

Whenever the score is below 8, "adjustments" MUST name at least one knob change that
addresses the biggest flaw (for example a table that leaves the bottom of the card empty
needs a larger table_row_height and font_scale). Only a picture you score 8 or higher may
have empty adjustments.

Reply with ONLY a JSON object:
{{"score": <1-10>, "criteria": {{"<checklist name>": <1-10>, ...}}, "flaws": ["..."],
 "fixes": ["..."], "adjustments": {{"knob": value}}}}
"""


def fallback_adjustments(visual: Chart | Table, style: Style) -> dict[str, Any]:
    """The knob step the loop takes when a low grade comes with no adjustments: a bigger,
    fuller picture. Empty once every knob it moves is at its ceiling (the loop then ends)."""
    if isinstance(visual, Table):
        keys = ("table_row_height", "font_scale")
    else:
        keys = ("row_pitch", "bar_height", "font_scale")
    step = {"table_row_height": 0.03, "font_scale": 0.15, "row_pitch": 0.03, "bar_height": 0.12}
    out: dict[str, Any] = {}
    for key in keys:
        current = getattr(style, key)
        hi = Style.RANGES[key][1]
        if current < hi:
            out[key] = min(hi, current + step[key])
    return out


def build_user_prompt(visual: Chart | Table, style: Style, previous: ImageGrade | None) -> str:
    kind = "table" if isinstance(visual, Table) else "bar chart"
    parts = [
        f"This is a {kind}. Its spec (the text the renderer was given):",
        json.dumps(visual.to_dict(), ensure_ascii=False),
        "Current layout knobs:",
        json.dumps(style.to_dict()),
    ]
    if previous is not None:
        parts += [
            f"Your previous grade for the last render was {previous.score}/10 with flaws: "
            + "; ".join(previous.flaws)
            + ". The knob changes you asked for were applied. Grade this new render.",
        ]
    parts.append("Grade the attached image.")
    return "\n\n".join(parts)


def call_grader(image_path: Path, system: str, user: str, model: str) -> str:
    """The single network call: the PNG plus the prompts to the model, its reply text back.
    On the claude_code backend the CLI reads the file itself (tools=["Read"])."""
    from dotenv import load_dotenv

    load_dotenv()
    try:
        import config as root_config

        cfg = root_config.load_config()
    except Exception:
        cfg = {}
    if claude_cli.llm_backend(cfg) == claude_cli.CLAUDE_CODE:
        prompt = f"{user}\n\nThe image is the file at: {image_path.resolve()}\nRead it first."
        return claude_cli.run_claude(prompt, system=system, model=model, cfg=cfg, tools=["Read"])

    import anthropic  # imported here so tests that mock this function never touch the SDK

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise RuntimeError("ANTHROPIC_API_KEY is not set (put it in .env)")
    client = anthropic.Anthropic(api_key=api_key)
    data = base64.standard_b64encode(image_path.read_bytes()).decode("ascii")
    resp = client.messages.create(
        model=model,
        max_tokens=MAX_TOKENS,
        system=system,
        messages=[
            {
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {"type": "base64", "media_type": "image/png", "data": data},
                    },
                    {"type": "text", "text": user},
                ],
            }
        ],
    )
    return "".join(getattr(block, "text", "") for block in resp.content)


def _texts(value: Any) -> list[str]:
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        return []
    out = [str(x).strip()[:MAX_TEXT_CHARS] for x in value if str(x).strip()]
    return out[:MAX_TEXT_ITEMS]


def parse_grade(text: str, style: Style, *, model: str = "", iteration: int = 1) -> ImageGrade:
    """The model's reply as an ImageGrade. Adjustments are clamped through `Style.apply` so
    only known knobs within range survive. Raises GraderError on an unusable reply."""
    try:
        data = parse_json_response(text)
    except ValueError as exc:
        raise GraderError(f"grader reply is not JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise GraderError("grader reply is not an object")
    try:
        score = int(round(float(data.get("score"))))
    except (TypeError, ValueError) as exc:
        raise GraderError("grader reply has no numeric score") from exc
    score = min(max(score, 1), 10)
    criteria: dict[str, int] = {}
    raw_crit = data.get("criteria")
    if isinstance(raw_crit, dict):
        for name, value in raw_crit.items():
            if name in CHECKLIST:
                try:
                    criteria[name] = min(max(int(round(float(value))), 1), 10)
                except (TypeError, ValueError):
                    continue
    raw_adj = data.get("adjustments") or {}
    adjusted = style.apply(raw_adj if isinstance(raw_adj, dict) else {})
    changes = {k: v for k, v in adjusted.to_dict().items() if v != getattr(style, k)}
    return ImageGrade(
        score=score,
        criteria=criteria,
        flaws=_texts(data.get("flaws")),
        fixes=_texts(data.get("fixes")),
        adjustments=changes,
        model=model,
        iteration=iteration,
        raw=text,
    )


def grade_image(
    image_path: Path,
    visual: Chart | Table,
    style: Style,
    *,
    model: str,
    iteration: int = 1,
    previous: ImageGrade | None = None,
) -> ImageGrade:
    """One grader call for one render. Raises GraderError / whatever the backend raises;
    the caller (approval_queue.images) treats any failure as 'keep this render'."""
    user = build_user_prompt(visual, style, previous)
    text = call_grader(Path(image_path), SYSTEM_PROMPT, user, model)
    grade = parse_grade(text, style, model=model, iteration=iteration)
    log.info(
        "image %s: grader %s scored %d/10 (iteration %d)%s",
        image_path.name,
        model,
        grade.score,
        iteration,
        f", adjustments {grade.adjustments}" if grade.adjustments else "",
    )
    return grade


__all__ = [
    "CHECKLIST",
    "CRITERIA",
    "DEFAULT_MAX_ITERATIONS",
    "DEFAULT_MIN_SCORE",
    "GraderError",
    "GraderSettings",
    "ImageGrade",
    "build_user_prompt",
    "call_grader",
    "fallback_adjustments",
    "grade_image",
    "grader_settings",
    "parse_grade",
]
