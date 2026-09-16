"""Output schema for the drafting model.

Pydantic-free on purpose: a dataclass, a JSON-schema dict handed to the model,
and a validator that turns raw JSON into the dataclass or raises SchemaError.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Any

from draft.chart import (
    CHART_JSON_SCHEMA,
    TABLE_JSON_SCHEMA,
    Chart,
    ChartError,
    Table,
    validate_chart,
    validate_table,
)

MAX_POST_CHARS = 280
URL_CHARS = 23  # X wraps every URL through t.co, which always counts as 23 chars
THREAD_MIN = 3
THREAD_MAX = 6
CONFIDENCE_LEVELS = ("high", "medium", "low")

# Step 9 phase four: the shape of a draft is a gene, not a constant. A Format says what
# validate_output and check_hard_rules require; with no Format they require the phase-one
# physics (a 3-6 post thread with exactly one visual) exactly as before.
SHAPES = ("thread", "single", "long")
SHAPE_THREAD, SHAPE_SINGLE, SHAPE_LONG = SHAPES
ANCHOR_WORDS = ("first", "last", "middle")
MAX_VISUALS = 2
DEFAULT_LONG_MAX_CHARS = 4000

# A single or long post never carries the source URL itself: the link lives in a second,
# threaded post so the opener stays link-free (X shows a post with an outbound link to
# fewer non-followers). So every non-thread shape is exactly two posts: the post, then the
# link post.
LINK_POST_SHAPE_POSTS = 2


@dataclass(frozen=True)
class Format:
    """What a draft must look like. `anchors` has one word per visual (first | last |
    middle) resolved against the thread length by `resolve_anchors`; a single or long post
    anchors everything to post 1 (its second post is the link post, never a picture).
    `max_chars` is the per-post limit (280 unless long).

    A single or long shape is always two posts (`LINK_POST_SHAPE_POSTS`): whatever
    min_posts/max_posts a caller or a stored format genome passes is normalised to that,
    since the body post is followed by the post that holds the primary source URL."""

    shape: str = SHAPE_THREAD
    min_posts: int = THREAD_MIN
    max_posts: int = THREAD_MAX
    visuals: int = 1
    anchors: tuple[str, ...] = ("first",)
    max_chars: int = MAX_POST_CHARS

    def __post_init__(self) -> None:
        if self.shape not in SHAPES:
            raise ValueError(f"shape must be one of {SHAPES}, got {self.shape!r}")
        if not 0 <= self.visuals <= MAX_VISUALS:
            raise ValueError(f"visuals must be 0-{MAX_VISUALS}, got {self.visuals}")
        if len(self.anchors) != self.visuals:
            raise ValueError("one anchor per visual")
        if any(a not in ANCHOR_WORDS for a in self.anchors):
            raise ValueError(f"anchors must be in {ANCHOR_WORDS}")
        if self.shape == SHAPE_THREAD:
            if not 2 <= self.min_posts <= self.max_posts <= THREAD_MAX:
                raise ValueError("a thread needs 2 <= min_posts <= max_posts <= 6")
            if self.max_chars != MAX_POST_CHARS:
                raise ValueError("thread posts are 280 characters")
        else:
            # The body post plus the link post, whatever the caller asked for.
            object.__setattr__(self, "min_posts", LINK_POST_SHAPE_POSTS)
            object.__setattr__(self, "max_posts", LINK_POST_SHAPE_POSTS)
            if self.shape == SHAPE_SINGLE:
                if self.max_chars != MAX_POST_CHARS:
                    raise ValueError("a single post is 280 characters")
            elif self.max_chars <= MAX_POST_CHARS:
                raise ValueError("a long post has max_chars above 280")

    @property
    def is_thread(self) -> bool:
        return self.shape == SHAPE_THREAD

    @property
    def has_link_post(self) -> bool:
        """True when the last post is the link post: a single or long post keeps the source
        URL out of the body and puts it in a second, threaded post of its own."""
        return not self.is_thread

    def resolve_anchors(self, n_posts: int) -> list[int]:
        """1-based post index per visual for a thread of n_posts."""
        out = []
        if self.has_link_post:
            # Never the link post: every picture sits on the body post.
            return [1] * self.visuals
        for a in self.anchors:
            if a == "first" or n_posts <= 1:
                out.append(1)
            elif a == "last":
                out.append(n_posts)
            else:
                out.append((n_posts + 1) // 2)
        return out

    def to_dict(self) -> dict[str, Any]:
        return {
            "shape": self.shape,
            "min_posts": self.min_posts,
            "max_posts": self.max_posts,
            "visuals": self.visuals,
            "anchors": list(self.anchors),
            "max_chars": self.max_chars,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any] | None) -> Format | None:
        if not d:
            return None
        return cls(
            shape=str(d.get("shape", SHAPE_THREAD)),
            min_posts=int(d.get("min_posts", THREAD_MIN)),
            max_posts=int(d.get("max_posts", THREAD_MAX)),
            visuals=int(d.get("visuals", 1)),
            anchors=tuple(d.get("anchors") or ()),
            max_chars=int(d.get("max_chars", MAX_POST_CHARS)),
        )


DEFAULT_FORMAT = Format()  # the phase-one physics


def single_format() -> Format:
    return Format(shape=SHAPE_SINGLE)


def long_format(max_chars: int = DEFAULT_LONG_MAX_CHARS) -> Format:
    return Format(shape=SHAPE_LONG, max_chars=max_chars)


_URL_RE = re.compile(r"https?://\S+")


class SchemaError(ValueError):
    """Raised when model output does not match the expected schema."""


@dataclass
class Claim:
    claim: str
    confidence: str  # one of CONFIDENCE_LEVELS


@dataclass
class Draft:
    thread: list[str]
    suggested_visual: str
    why_it_matters: str
    claims_to_verify: list[Claim] = field(default_factory=list)
    # The visual (draft/chart.py). validate_output requires exactly one of chart/table, so a
    # draft always comes with a picture spec; the dataclass keeps both optional for the
    # stand-ins the queue and the voice loop build (a human-edited text, a failed draft).
    chart: Chart | None = None
    # Comparison table (draft/chart.py:Table), the alternative to a chart. Its cells are
    # web-verified by step 2b before anything is rendered.
    table: Table | None = None
    # Phase four: the draft's shape and where each picture goes (1-based post index per
    # visual, the first visual first). Older rows: thread, anchored to post 1.
    shape: str = SHAPE_THREAD
    anchors: list[int] = field(default_factory=lambda: [1])
    max_chars: int = MAX_POST_CHARS
    # Further visuals beyond the first: charts only (verbatim numbers, no web check).
    extra_visuals: list[Chart] = field(default_factory=list)
    # How many visuals the format asked for when the draft was written (a reviewer may drop
    # one later); None for a draft from before phase four, which means the default physics.
    wanted_visuals: int | None = None

    @property
    def visual(self) -> Chart | Table | None:
        return self.chart if self.chart is not None else self.table

    @property
    def visuals(self) -> list[Chart | Table]:
        first = [self.visual] if self.visual is not None else []
        return first + list(self.extra_visuals)

    def format_dict(self) -> dict[str, Any]:
        """What drafts.format_json stores (phase four)."""
        return {
            "shape": self.shape,
            "anchors": list(self.anchors),
            "max_chars": self.max_chars,
            "wanted_visuals": self.wanted_visuals,
        }

    def apply_format_dict(self, d: dict[str, Any] | None) -> None:
        """Restore the fields format_dict stored; a missing or empty dict leaves the
        pre-phase-four defaults."""
        if not d:
            return
        self.shape = str(d.get("shape") or SHAPE_THREAD)
        anchors = d.get("anchors")
        if isinstance(anchors, list) and anchors:
            self.anchors = [int(a) for a in anchors]
        self.max_chars = int(d.get("max_chars") or MAX_POST_CHARS)
        wv = d.get("wanted_visuals")
        self.wanted_visuals = int(wv) if wv is not None else None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def all_posts(self) -> list[str]:
        return list(self.thread)


OUTPUT_JSON_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "thread",
        "suggested_visual",
        "why_it_matters",
        "claims_to_verify",
    ],
    "properties": {
        "thread": {
            "type": "array",
            "minItems": THREAD_MIN,
            "maxItems": THREAD_MAX,
            "items": {"type": "string"},
            "description": "3-6 posts. Each <= 280 chars. Last post contains the source URL.",
        },
        "suggested_visual": {
            "type": "string",
            "description": "One line for the reviewer saying what the chart or table shows.",
        },
        "why_it_matters": {
            "type": "string",
            "description": "The interpretation, in one or two sentences, for the human reviewer.",
        },
        "chart": CHART_JSON_SCHEMA,
        "table": TABLE_JSON_SCHEMA,
        "visuals": {
            "type": "array",
            "maxItems": MAX_VISUALS - 1,
            "items": CHART_JSON_SCHEMA,
            "description": "Further charts beyond the first visual, only when the format "
            "asks for more than one picture. Each is checked like 'chart'.",
        },
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


def validate_output(data: Any, fmt: Format | None = None) -> Draft:
    """Validate raw JSON (already parsed) and return a Draft.

    Checks structure and types only, plus the visual count the format asks for. Content
    rules (character limit, URL present, preprint label, number verification) live in
    draft.drafter.check_hard_rules. With fmt None the phase-one physics apply: a 3-6 post
    thread with exactly one of chart/table.
    """
    fmt = fmt or DEFAULT_FORMAT
    if not isinstance(data, dict):
        raise SchemaError("output must be a JSON object")
    missing = [k for k in OUTPUT_JSON_SCHEMA["required"] if k not in data]
    if missing:
        raise SchemaError(f"missing keys: {', '.join(missing)}")
    extra = set(data) - set(OUTPUT_JSON_SCHEMA["properties"])
    if extra:
        raise SchemaError(f"unexpected keys: {', '.join(sorted(extra))}")

    thread = data["thread"]
    if not isinstance(thread, list) or not all(isinstance(p, str) for p in thread):
        raise SchemaError("'thread' must be a list of strings")
    thread = [p.strip() for p in thread]
    if any(not p for p in thread):
        raise SchemaError("'thread' contains an empty post")
    lo, hi = fmt.min_posts, fmt.max_posts
    if not lo <= len(thread) <= hi:
        if fmt.is_thread:
            raise SchemaError(f"'thread' must have {lo}-{hi} posts, got {len(thread)}")
        raise SchemaError(
            f"a {fmt.shape} post is {lo} posts (the post, then a post holding only the "
            f"primary source URL), got {len(thread)}"
        )

    try:
        chart = validate_chart(data.get("chart"))
        table = validate_table(data.get("table"))
        extras_raw = data.get("visuals") or []
        if not isinstance(extras_raw, list):
            raise SchemaError("'visuals' must be a list")
        extras = [validate_chart(v) for v in extras_raw]
    except ChartError as exc:
        raise SchemaError(str(exc)) from exc
    if any(v is None for v in extras):
        raise SchemaError("'visuals' contains an empty chart")
    if chart is not None and table is not None:
        raise SchemaError("give a chart or a table, not both")
    n_visuals = (1 if chart is not None or table is not None else 0) + len(extras)
    if fmt.visuals == 0 and n_visuals:
        raise SchemaError("this format carries no visual: give neither chart nor table")
    if fmt.visuals >= 1 and chart is None and table is None:
        raise SchemaError("every draft needs a visual: give a chart or a table")
    if n_visuals != fmt.visuals:
        raise SchemaError(
            f"this format carries {fmt.visuals} visual(s), got {n_visuals} "
            "(the first is 'chart' or 'table', any further one goes in 'visuals')"
        )

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
        thread=thread,
        suggested_visual=_require_str(data, "suggested_visual"),
        why_it_matters=_require_str(data, "why_it_matters"),
        claims_to_verify=claims,
        chart=chart,
        table=table,
        shape=fmt.shape,
        anchors=fmt.resolve_anchors(len(thread)),
        max_chars=fmt.max_chars,
        extra_visuals=[v for v in extras if v is not None],
        wanted_visuals=fmt.visuals,
    )
