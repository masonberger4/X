"""Output schema for the drafting model.

Pydantic-free on purpose: a dataclass, a JSON-schema dict handed to the model,
and a validator that turns raw JSON into the dataclass or raises SchemaError.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Any

from draft.chart import CHART_JSON_SCHEMA, Chart, ChartError, validate_chart

MAX_POST_CHARS = 280
URL_CHARS = 23  # X wraps every URL through t.co, which always counts as 23 chars
THREAD_MIN = 3
THREAD_MAX = 6
CONFIDENCE_LEVELS = ("high", "medium", "low")

_URL_RE = re.compile(r"https?://\S+")


class SchemaError(ValueError):
    """Raised when model output does not match the expected schema."""


@dataclass
class Claim:
    claim: str
    confidence: str  # one of CONFIDENCE_LEVELS


@dataclass
class Draft:
    single_post: str
    thread: list[str]
    suggested_visual: str
    why_it_matters: str
    claims_to_verify: list[Claim] = field(default_factory=list)
    # Optional chart spec (draft/chart.py). None: no image. Never required, so an older
    # model output without the key still validates.
    chart: Chart | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def all_posts(self) -> list[str]:
        return [self.single_post, *self.thread]


OUTPUT_JSON_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "single_post",
        "thread",
        "suggested_visual",
        "why_it_matters",
        "claims_to_verify",
    ],
    "properties": {
        "single_post": {
            "type": "string",
            "description": "One standalone post, <= 280 chars counting URLs as 23.",
        },
        "thread": {
            "type": "array",
            "minItems": THREAD_MIN,
            "maxItems": THREAD_MAX,
            "items": {"type": "string"},
            "description": "3-6 posts. Each <= 280 chars. Last post contains the source URL.",
        },
        "suggested_visual": {
            "type": "string",
            "description": "Short description of a chart, figure or image to attach.",
        },
        "why_it_matters": {
            "type": "string",
            "description": "The interpretation, in one or two sentences, for the human reviewer.",
        },
        "chart": CHART_JSON_SCHEMA,
        "claims_to_verify": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["claim", "confidence"],
                "properties": {
                    "claim": {"type": "string"},
                    "confidence": {"type": "string", "enum": list(CONFIDENCE_LEVELS)},
                },
            },
        },
    },
}


def tweet_length(text: str) -> int:
    """Length as X counts it: every URL is 23 characters regardless of its real length."""
    stripped = _URL_RE.sub("", text)
    n_urls = len(_URL_RE.findall(text))
    return len(stripped) + n_urls * URL_CHARS


def _require_str(data: dict[str, Any], key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str):
        raise SchemaError(f"'{key}' must be a string")
    return value.strip()


def validate_output(data: Any) -> Draft:
    """Validate raw JSON (already parsed) and return a Draft.

    Checks structure and types only. Content rules (280 chars, URL present,
    preprint label, number verification) live in draft.drafter.check_hard_rules.
    """
    if not isinstance(data, dict):
        raise SchemaError("output must be a JSON object")
    missing = [k for k in OUTPUT_JSON_SCHEMA["required"] if k not in data]
    if missing:
        raise SchemaError(f"missing keys: {', '.join(missing)}")
    extra = set(data) - set(OUTPUT_JSON_SCHEMA["properties"])
    if extra:
        raise SchemaError(f"unexpected keys: {', '.join(sorted(extra))}")

    single_post = _require_str(data, "single_post")
    if not single_post:
        raise SchemaError("'single_post' is empty")

    thread = data["thread"]
    if not isinstance(thread, list) or not all(isinstance(p, str) for p in thread):
        raise SchemaError("'thread' must be a list of strings")
    thread = [p.strip() for p in thread]
    if any(not p for p in thread):
        raise SchemaError("'thread' contains an empty post")
    if not THREAD_MIN <= len(thread) <= THREAD_MAX:
        raise SchemaError(f"'thread' must have {THREAD_MIN}-{THREAD_MAX} posts, got {len(thread)}")

    try:
        chart = validate_chart(data.get("chart"))
    except ChartError as exc:
        raise SchemaError(str(exc)) from exc

    claims_raw = data["claims_to_verify"]
    if not isinstance(claims_raw, list):
        raise SchemaError("'claims_to_verify' must be a list")
    claims: list[Claim] = []
    for i, c in enumerate(claims_raw):
        if not isinstance(c, dict):
            raise SchemaError(f"claims_to_verify[{i}] must be an object")
        claim = c.get("claim")
        conf = c.get("confidence")
        if not isinstance(claim, str) or not claim.strip():
            raise SchemaError(f"claims_to_verify[{i}].claim must be a non-empty string")
        if conf not in CONFIDENCE_LEVELS:
            raise SchemaError(
                f"claims_to_verify[{i}].confidence must be one of {CONFIDENCE_LEVELS}, got {conf!r}"
            )
        claims.append(Claim(claim=claim.strip(), confidence=conf))

    return Draft(
        single_post=single_post,
        thread=thread,
        suggested_visual=_require_str(data, "suggested_visual"),
        why_it_matters=_require_str(data, "why_it_matters"),
        claims_to_verify=claims,
        chart=chart,
    )
