"""Builds the drafting prompt from voice.md, the item, and the scorer's suggested angle."""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

from draft.schema import MAX_POST_CHARS, OUTPUT_JSON_SCHEMA, THREAD_MAX, THREAD_MIN, URL_CHARS

VOICE_PATH = Path(__file__).with_name("voice.md")

PREPRINT_SOURCES = ("biorxiv", "medrxiv")
PREPRINT_LABEL = "preprint"

HARD_RULES = f"""HARD RULES. A draft that breaks any of these is discarded automatically.
1. No medical advice and no treatment recommendations. Describe evidence; never tell
   anyone what they or their doctor should do.
2. The primary source URL must appear verbatim in single_post AND in the last thread post.
3. If the source is a preprint (bioRxiv / medRxiv), the word "{PREPRINT_LABEL}" must appear in
   single_post and in the first thread post.
4. Every number you write must appear verbatim in the source abstract or title. Do not
   round, convert units, compute differences or percentages, or infer sample sizes.
   If a number you want is not in the abstract, leave it out.
5. Every post is at most {MAX_POST_CHARS} characters. Count every URL as {URL_CHARS} characters.
6. The thread has {THREAD_MIN} to {THREAD_MAX} posts.
7. Every post must contain an interpretation, not just a restatement (see voice guide).
8. Any claim that goes beyond what the abstract states goes into claims_to_verify with an
   honest confidence level.
"""


@lru_cache(maxsize=1)
def load_voice_guide(path: Path = VOICE_PATH) -> str:
    return path.read_text(encoding="utf-8")


def is_preprint(source: str | None) -> bool:
    """Step 1 names preprint sources like 'biorxiv_cancer_biology' / 'medrxiv_oncology'."""
    name = (source or "").strip().lower()
    return any(p in name for p in PREPRINT_SOURCES)


def build_system_prompt() -> str:
    return (
        "You draft posts for a cancer-research X account. A human reviews and edits every "
        "draft before anything is published; nothing you write is posted automatically.\n\n"
        "Follow the voice guide exactly.\n\n"
        "=== VOICE GUIDE ===\n"
        f"{load_voice_guide()}\n"
        "=== END VOICE GUIDE ===\n\n"
        f"{HARD_RULES}\n"
        "Respond with a single JSON object and nothing else, matching this JSON schema:\n"
        f"{json.dumps(OUTPUT_JSON_SCHEMA, indent=2)}"
    )


def build_user_prompt(
    *,
    title: str,
    abstract: str,
    url: str,
    source: str,
    published_at: str | None = None,
    suggested_angle: str | None = None,
    rationale: str | None = None,
) -> str:
    parts = [
        f"SOURCE: {source}" + ("  (THIS IS A PREPRINT)" if is_preprint(source) else ""),
        f"PRIMARY SOURCE URL: {url}",
        f"PUBLISHED: {published_at or 'unknown'}",
        f"TITLE: {title}",
        "",
        "ABSTRACT:",
        abstract or "(no abstract available; do not invent findings)",
        "",
    ]
    if rationale:
        parts.append(f"SCORER RATIONALE: {rationale}")
    if suggested_angle:
        parts.append(f"SUGGESTED ANGLE: {suggested_angle}")
    parts.append("")
    parts.append("Draft the single_post and the thread now. Output JSON only.")
    return "\n".join(parts)


def build_prompt(
    *,
    title: str,
    abstract: str,
    url: str,
    source: str,
    published_at: str | None = None,
    suggested_angle: str | None = None,
    rationale: str | None = None,
) -> tuple[str, str]:
    """Return (system_prompt, user_prompt)."""
    return build_system_prompt(), build_user_prompt(
        title=title,
        abstract=abstract,
        url=url,
        source=source,
        published_at=published_at,
        suggested_angle=suggested_angle,
        rationale=rationale,
    )
