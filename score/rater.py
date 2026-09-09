"""Second-opinion rater: a stronger model rates digest entries 1-5 the way a human does
with `digest.py --rate`, so the human can compare, disagree, and tune faster.

Ratings are stored with rater='auto:<model>' and are never confused with human
ratings (the feedback report and the rubric-tuning workflow read human rows only).
The single network call is `call_model`, which routes through `claude_cli.run_claude`
on the claude_code backend or the Anthropic API otherwise; tests replace it.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import claude_cli

log = logging.getLogger(__name__)

MAX_TOKENS = 400
SCALE = """Rating scale (how the account's human editor uses it):
5 = would post about this today: moves a thesis, names a public company or catalyst, fits the beat
4 = worth a post, not urgent
3 = interesting but probably would not post
2 = marginal
1 = noise; should not have scored this high"""

SYSTEM = f"""You are a PhD-level immuno-oncology analyst at a hedge fund, acting as the editor of an X account about the business and investing side of immuno-oncology biotech: CAR-T and cell therapy, T-cell engagers and bispecifics, adjacent IO science; trial results and what they mean, upcoming catalysts for public companies, M&A and financing. The account never gives medical or investment advice.

For the item you are given, decide how the editor would rate it for posting.

{SCALE}

Judge on: does it change a thesis or a competitive picture, is there a public company or a dated catalyst, is the evidence strong enough to say something non-obvious, and is it on the beat. The editor's calibration from real ratings: a DATED catalyst for a public company on the beat, with enough context to explain the disease, the technology and what success would change, is a 5 even before data exist (a pre-data announcement is a 1-2 only when it is off the beat or carries no context). A negative or informative early readout from a named public sponsor that reads across to competitors is a 5. Items with no business or investment implication at all (guidelines, grading criteria, consensus statements, reviews) are a 2 however strong the science. A well-run trial in an unrelated modality is a 2. Never infer a company or sponsor that the item text does not name; if the sponsor is not stated, say so in the note rather than guessing.

Reply with ONLY a JSON object: {{"rating": <1-5>, "note": "<one sentence, <= 200 chars, the reason>"}}"""


@dataclass
class AutoRating:
    rating: int
    note: str


def build_user(entry: dict[str, Any]) -> str:
    parts = [
        f"SOURCE: {entry.get('source', '?')}",
        f"PUBLISHED: {entry.get('published_at') or 'unknown'}",
        f"TITLE: {entry.get('title', '')}",
        f"SCORER TOTAL: {entry.get('total', '?')}/50 (hype {entry.get('hype_risk', '?')}/10)",
        f"SCORER RATIONALE: {entry.get('rationale') or ''}",
        f"SCORER ANGLE: {entry.get('suggested_angle') or ''}",
        "",
        "ABSTRACT:",
        (entry.get("abstract") or "(none)")[:3000],
        "",
        "Rate it. JSON only.",
    ]
    return "\n".join(parts)


def parse_reply(text: str) -> AutoRating:
    text = text.strip()
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if not m:
        raise ValueError(f"no JSON object in reply: {text[:120]!r}")
    data = json.loads(m.group(0))
    rating = int(data["rating"])
    if not 1 <= rating <= 5:
        raise ValueError(f"rating out of range: {rating}")
    note = str(data.get("note") or "").strip()[:200]
    return AutoRating(rating=rating, note=note)


def call_model(system: str, user: str, model: str, effort: str | None, cfg: dict) -> str:
    """The single network call (claude_code backend or Anthropic API)."""
    if claude_cli.llm_backend(cfg) == claude_cli.CLAUDE_CODE:
        return claude_cli.run_claude(user, system=system, model=model, cfg=cfg, effort=effort)

    import anthropic  # local import so tests never touch the SDK

    kwargs: dict[str, Any] = dict(
        model=model,
        max_tokens=MAX_TOKENS,
        system=system,
        messages=[{"role": "user", "content": user}],
    )
    if effort:
        kwargs["output_config"] = {"effort": effort}
    resp = anthropic.Anthropic().messages.create(**kwargs)
    return "".join(getattr(b, "text", "") for b in resp.content)


CallFn = Callable[[str, str, str, str | None, dict], str]


def rate_entry(entry: dict[str, Any], cfg: dict[str, Any], call: CallFn = call_model) -> AutoRating:
    models = cfg.get("models") or {}
    model = str(models.get("rater") or "").strip()
    if not model:
        raise ValueError("config.yaml models.rater is not set")
    effort = str(models.get("rater_effort") or "").strip().lower() or None
    reply = call(SYSTEM, build_user(entry), model, effort, cfg)
    return parse_reply(reply)


def rater_name(cfg: dict[str, Any]) -> str:
    return f"auto:{(cfg.get('models') or {}).get('rater', '?')}"
