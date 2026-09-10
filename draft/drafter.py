"""Calls the Anthropic API to draft one item, then enforces the hard rules in code.

All network I/O goes through call_anthropic(); tests replace it. With
`models.backend: claude_code` (or LLM_BACKEND=claude_code) call_anthropic runs the
Claude Code CLI via `claude_cli.run_claude` instead; the JSON parsing, schema check
and hard rules below are the same on both backends.
"""

from __future__ import annotations

import json
import logging
import os
import random
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from dotenv import load_dotenv

import claude_cli
from draft.prompt import (
    PREPRINT_LABEL,
    ClaimProblem,
    build_prompt,
    build_revision_user_prompt,
    is_preprint,
)
from draft.schema import (
    MAX_POST_CHARS,
    URL_CHARS,
    Claim,
    Draft,
    SchemaError,
    tweet_length,
    validate_output,
)
from draft.settings import load_draft_config

log = logging.getLogger(__name__)

MAX_TOKENS = 2048
MAX_ATTEMPTS = 4
BACKOFF_BASE_SECONDS = 2.0

# Phrases that read as medical advice or treatment recommendations. Case-insensitive.
MEDICAL_ADVICE_PATTERNS = (
    r"\bask your (doctor|oncologist|physician)\b",
    r"\btalk to your (doctor|oncologist|physician)\b",
    r"\bpatients should\b",
    r"\byou should (take|try|switch|stop|start|ask|consider)\b",
    r"\bwe recommend\b",
    r"\bis recommended for patients\b",
    r"\bshould (be|now be) (given|prescribed|offered) to\b",
    r"\bfirst[- ]line (choice|option) for you\b",
    r"\bconsider (taking|switching to|starting|stopping)\b",
)
_ADVICE_RE = re.compile("|".join(MEDICAL_ADVICE_PATTERNS), re.IGNORECASE)

# Phrases that read as investment advice: a directional call on a security, a price
# target, or a promised return. Describing a thesis, a valuation or a risk is fine.
INVESTMENT_ADVICE_PATTERNS = (
    r"\b(buy|sell|short|accumulate|dump|hold)\s+(the\s+)?(stock|shares|calls|puts)\b",
    r"\b(buy|sell|short|accumulate|dump)\s+\$[A-Za-z]{1,5}\b",
    r"\b(strong|clear|obvious)\s+(buy|sell|short)\b",
    r"\b(you|investors|traders)\s+should\s+(buy|sell|short|hold|avoid|add|trim)\b",
    r"\bprice\s+target\b",
    r"\bwill\s+(double|triple|10x|moon)\b",
    r"\bto\s+the\s+moon\b",
    r"\b(easy|free)\s+money\b",
    r"\bguaranteed\s+(return|gain|profit|win)\b",
    r"\bcan'?t\s+lose\b",
    r"\bload\s+up\s+on\b",
)
_INVEST_RE = re.compile("|".join(INVESTMENT_ADVICE_PATTERNS), re.IGNORECASE)

# Numbers: integers with optional thousands separators, decimals, and percentages.
_NUMBER_RE = re.compile(r"(?<![\w/.])(\d{1,3}(?:,\d{3})+|\d+(?:\.\d+)?)(\s?%)?")
_URL_RE = re.compile(r"https?://\S+")


class DraftRejected(Exception):
    """The model produced a draft that broke a hard rule after all attempts."""

    def __init__(self, reasons: list[str]):
        super().__init__("; ".join(reasons))
        self.reasons = reasons


@dataclass
class DraftResult:
    draft: Draft
    model: str
    attempts: int
    flagged_numbers: list[str] = field(default_factory=list)


def model_name() -> str:
    """Drafting model: DRAFT_MODEL env, else root config.yaml models.drafter (if the human has
    added that key), else draft/config.yaml `model`. No model ID is hardcoded here."""
    env = os.environ.get("DRAFT_MODEL", "").strip()
    if env:
        return env
    try:
        import config as root_config

        configured = (root_config.load_config().get("models") or {}).get("drafter")
    except Exception:  # root config.yaml missing or unreadable
        configured = None
    if configured:
        return str(configured)
    return str(load_draft_config()["model"])


def _root_config() -> dict:
    try:
        import config as root_config

        return root_config.load_config()
    except Exception:  # root config.yaml missing or unreadable
        return {}


def call_anthropic(system: str, user: str, model: str) -> str:
    """The single network call. Returns the raw text of the first content block, or, on the
    claude_code backend, the CLI's result text."""
    load_dotenv()
    cfg = _root_config()
    if claude_cli.llm_backend(cfg) == claude_cli.CLAUDE_CODE:
        return claude_cli.run_claude(user, system=system, model=model, cfg=cfg)

    import anthropic  # imported here so tests that mock this function never touch the SDK

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise RuntimeError("ANTHROPIC_API_KEY is not set (put it in .env)")
    client = anthropic.Anthropic(api_key=api_key)
    resp = client.messages.create(
        model=model,
        max_tokens=MAX_TOKENS,
        system=system,
        messages=[{"role": "user", "content": user}],
    )
    return "".join(getattr(block, "text", "") for block in resp.content)


def parse_json_response(text: str) -> object:
    """Parse the model's reply, tolerating ```json fences and leading prose."""
    text = text.strip()
    fence = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.DOTALL)
    if fence:
        text = fence.group(1)
    elif not text.startswith("{"):
        start, end = text.find("{"), text.rfind("}")
        if start != -1 and end > start:
            text = text[start : end + 1]
    return json.loads(text)


# ---------------------------------------------------------------------------
# Hard-rule checks (mirror of HARD_RULES in prompt.py)
# ---------------------------------------------------------------------------


def _url_in(text: str, url: str) -> bool:
    return url in text


def numbers_in(text: str) -> list[str]:
    """Numbers as written, e.g. '88%', '14.6', '1,200'. URLs are ignored."""
    text = _URL_RE.sub(" ", text)
    return [
        m.group(1) + (m.group(2).replace(" ", "") if m.group(2) else "")
        for m in _NUMBER_RE.finditer(text)
    ]


def _number_in_source(num: str, source_text: str) -> bool:
    """A number counts as verbatim if it (and its % sign, if any) appears in the source text."""
    bare = num.rstrip("%")
    if bare not in source_text:
        return False
    if num.endswith("%"):
        # Accept "88%", "88 %", "88 percent"
        return bool(re.search(re.escape(bare) + r"\s?(%|percent)", source_text))
    return True


def verify_numbers(draft: Draft, source_text: str) -> list[str]:
    """Return every number in the draft that does not appear verbatim in the source."""
    seen: set[str] = set()
    missing: list[str] = []
    for post in draft.all_posts():
        for num in numbers_in(post):
            if num in seen:
                continue
            seen.add(num)
            if not _number_in_source(num, source_text):
                missing.append(num)
    return missing


def check_hard_rules(draft: Draft, *, url: str, source: str) -> list[str]:
    """Violations that make a draft unusable. Empty list means the draft passes."""
    problems: list[str] = []
    for i, post in enumerate(draft.all_posts()):
        label = "single_post" if i == 0 else f"thread[{i - 1}]"
        n = tweet_length(post)
        if n > MAX_POST_CHARS:
            problems.append(f"{label} is {n} chars (> {MAX_POST_CHARS})")
        if _ADVICE_RE.search(post):
            problems.append(
                f"{label} reads as medical advice: {_ADVICE_RE.search(post).group(0)!r}"
            )
        if _INVEST_RE.search(post):
            problems.append(
                f"{label} reads as investment advice: {_INVEST_RE.search(post).group(0)!r}"
            )
    if not _url_in(draft.single_post, url):
        problems.append("single_post is missing the primary source URL")
    if not _url_in(draft.thread[-1], url):
        problems.append("last thread post is missing the primary source URL")
    if is_preprint(source):
        if PREPRINT_LABEL not in draft.single_post.lower():
            problems.append("preprint not labelled in single_post")
        if PREPRINT_LABEL not in draft.thread[0].lower():
            problems.append("preprint not labelled in first thread post")
    return problems


def flag_unverified_numbers(draft: Draft, source_text: str) -> list[str]:
    """Append a low-confidence claim for every number not found in the source. Returns them."""
    missing = verify_numbers(draft, source_text)
    existing = {c.claim for c in draft.claims_to_verify}
    for num in missing:
        claim = f"Number '{num}' does not appear in the source abstract"
        if claim not in existing:
            draft.claims_to_verify.append(Claim(claim=claim, confidence="low"))
    return missing


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

CallFn = Callable[[str, str, str], str]


def retry_prompt(user: str, reasons: list[str]) -> str:
    """The user prompt for a retry: the previous attempt's failures are appended so the
    model fixes them instead of guessing. Length failures are the common case and the model
    cannot count characters exactly, so it is told to leave a margin."""
    if not reasons:
        return user
    lines = "\n".join(f"- {r}" for r in reasons)
    return (
        f"{user}\n\nYOUR PREVIOUS ATTEMPT WAS DISCARDED for these hard-rule violations:\n{lines}\n"
        f"Write a new draft that fixes every one of them. Keep every post under "
        f"{MAX_POST_CHARS - 20} characters (counting a URL as {URL_CHARS}) to leave a margin."
    )


def _sleep_backoff(attempt: int, sleep: Callable[[float], None]) -> None:
    delay = BACKOFF_BASE_SECONDS * (2 ** (attempt - 1)) + random.uniform(0, 0.5)
    log.info("retrying in %.1fs", delay)
    sleep(delay)


def draft_item(
    *,
    title: str,
    abstract: str,
    url: str,
    source: str,
    published_at: str | None = None,
    suggested_angle: str | None = None,
    rationale: str | None = None,
    examples_block: str | None = None,
    call: CallFn = call_anthropic,
    model: str | None = None,
    max_attempts: int = MAX_ATTEMPTS,
    sleep: Callable[[float], None] = time.sleep,
) -> DraftResult:
    """Draft one item. Retries on API errors, bad JSON, schema errors and hard-rule failures.

    Raises DraftRejected if every attempt failed a hard rule, or re-raises the last API
    error if the API never returned usable output.

    examples_block (step 7): recent human edits, inserted into the system prompt before the
    hard rules. check_hard_rules and flag_unverified_numbers run on every output regardless.
    """
    model = model or model_name()
    system, user = build_prompt(
        title=title,
        abstract=abstract,
        url=url,
        source=source,
        published_at=published_at,
        suggested_angle=suggested_angle,
        rationale=rationale,
        examples_block=examples_block,
    )
    return _generate(
        system,
        user,
        model=model,
        url=url,
        source=source,
        source_text=f"{title}\n{abstract}",
        call=call,
        max_attempts=max_attempts,
        sleep=sleep,
    )


def revise_item(
    *,
    current: Draft,
    instructions: str | None,
    claim_problems: list[ClaimProblem] | None = None,
    title: str,
    abstract: str,
    url: str,
    source: str,
    published_at: str | None = None,
    suggested_angle: str | None = None,
    rationale: str | None = None,
    examples_block: str | None = None,
    call: CallFn = call_anthropic,
    model: str | None = None,
    max_attempts: int = MAX_ATTEMPTS,
    sleep: Callable[[float], None] = time.sleep,
) -> DraftResult:
    """Revise an existing draft: the model gets the same brief as draft_item plus the draft as
    it stands, the editor's instructions and the claims the fact-checker (step 2b) contradicted,
    and is asked to change only what those require. Raises ValueError when there is nothing to
    revise (no instructions and no claim problems). Everything after the call is identical to
    draft_item: schema check, hard rules in code, retries, number flagging."""
    if not (instructions or "").strip() and not claim_problems:
        raise ValueError("nothing to revise: give instructions or at least one claim problem")
    model = model or model_name()
    system, base_user = build_prompt(
        title=title,
        abstract=abstract,
        url=url,
        source=source,
        published_at=published_at,
        suggested_angle=suggested_angle,
        rationale=rationale,
        examples_block=examples_block,
    )
    user = build_revision_user_prompt(
        base_user_prompt=base_user,
        current=current.to_dict(),
        instructions=(instructions or "").strip() or None,
        claim_problems=claim_problems,
    )
    return _generate(
        system,
        user,
        model=model,
        url=url,
        source=source,
        source_text=f"{title}\n{abstract}",
        call=call,
        max_attempts=max_attempts,
        sleep=sleep,
    )


def _generate(
    system: str,
    user: str,
    *,
    model: str,
    url: str,
    source: str,
    source_text: str,
    call: CallFn,
    max_attempts: int,
    sleep: Callable[[float], None],
) -> DraftResult:
    """The shared attempt loop behind draft_item and revise_item."""
    last_reasons: list[str] = []
    last_exc: Exception | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            raw = call(system, retry_prompt(user, last_reasons), model)
        except Exception as exc:  # network / rate limit / SDK errors
            last_exc = exc
            log.warning("attempt %d: API call failed: %s", attempt, exc)
            if attempt < max_attempts:
                _sleep_backoff(attempt, sleep)
            continue
        try:
            draft = validate_output(parse_json_response(raw))
        except (json.JSONDecodeError, SchemaError) as exc:
            last_reasons = [f"invalid output: {exc}"]
            log.warning("attempt %d: %s", attempt, last_reasons[0])
            continue
        problems = check_hard_rules(draft, url=url, source=source)
        if problems:
            last_reasons = problems
            log.warning("attempt %d: hard-rule violations: %s", attempt, "; ".join(problems))
            continue
        flagged = flag_unverified_numbers(draft, source_text)
        if flagged:
            log.info("numbers not found in source, flagged for review: %s", flagged)
        return DraftResult(draft=draft, model=model, attempts=attempt, flagged_numbers=flagged)
    if last_reasons:
        log.error("draft rejected for %r after %d attempts: %s", url, max_attempts, last_reasons)
        raise DraftRejected(last_reasons)
    assert last_exc is not None
    raise last_exc
