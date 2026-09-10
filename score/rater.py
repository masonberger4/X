"""Second-opinion rater: a stronger model answers the same yes/no question the human editor
answers with `digest.py --rate` (post this story or not, and why), so the human can compare,
disagree, and tune faster.

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
from score import editorial

log = logging.getLogger(__name__)

MAX_TOKENS = 400
SCALE = f"""Decision (how the account's human editor answers it):
yes = would post about this: moves a thesis, names a public company or catalyst, fits the beat
no = would not post: off the beat, no business implication, weak or overhyped evidence, or already covered

{editorial.reasons_text()}"""

SYSTEM = f"""You are a PhD-level immuno-oncology analyst at a hedge fund, acting as the editor of an X account about the business and investing side of immuno-oncology biotech: CAR-T and cell therapy, T-cell engagers and bispecifics, adjacent IO science; trial results and what they mean, upcoming catalysts for public companies, M&A and financing. The account never gives medical or investment advice.

For the item you are given, decide whether the editor would post about it: yes or no.

{SCALE}

Judge on: does it change a thesis or a competitive picture, is there a public company or a dated catalyst, is the evidence strong enough to say something non-obvious, and is it on the beat. The editor's calibration from real decisions: a DATED catalyst for a public company on the beat, with enough context to explain the disease, the technology and what success would change, is a yes even before data exist (a pre-data announcement is a no only when it is off the beat or carries no context). A negative or informative early readout from a named public sponsor that reads across to competitors is a yes. Items with no business or investment implication at all (guidelines, grading criteria, consensus statements, reviews) are a no however strong the science. A well-run trial in an unrelated modality is a no. Never infer a company or sponsor that the item text does not name; if the sponsor is not stated, say so in the note rather than guessing.

Reply with ONLY a JSON object: {{"decision": "yes" | "no", "note": "<one sentence, <= 200 chars, starting with the deciding reason category>"}}"""


@dataclass
class AutoRating:
    decision: str  # editorial.YES or editorial.NO
    note: str

    @property
    def rating(self) -> int:
        """Numeric form stored in ratings.rating (yes = 5, no = 1)."""
        return editorial.rating_for(self.decision)


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
        "Decide: yes or no. JSON only.",
    ]
    return "\n".join(parts)


def parse_reply(text: str) -> AutoRating:
    text = text.strip()
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if not m:
        raise ValueError(f"no JSON object in reply: {text[:120]!r}")
    data = json.loads(m.group(0))
    raw = data.get("decision", data.get("rating"))  # "rating" 1-5: replies from the old scale
    try:
        decision = editorial.parse_decision(raw)
    except ValueError:
        raise ValueError(f"decision is not yes/no: {raw!r}") from None
    note = str(data.get("note") or "").strip()[:200]
    return AutoRating(decision=decision, note=note)


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
