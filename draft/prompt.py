"""Builds the drafting prompt from voice.md, the item, and the scorer's suggested angle."""

from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

from draft.schema import (
    MAX_POST_CHARS,
    OUTPUT_JSON_SCHEMA,
    SHAPE_SINGLE,
    THREAD_MAX,
    THREAD_MIN,
    URL_CHARS,
    Format,
)
from draft.tags import Handle, handles_block

VOICE_PATH = Path(__file__).with_name("voice.md")

PREPRINT_SOURCES = ("biorxiv", "medrxiv")
PREPRINT_LABEL = "preprint"


def _shape_rules(fmt: Format | None) -> tuple[str, str, str]:
    """(rule 5, rule 6, rule 9's first sentence) for the format. None = phase-one physics."""
    if fmt is None or fmt.is_thread:
        lo, hi = (THREAD_MIN, THREAD_MAX) if fmt is None else (fmt.min_posts, fmt.max_posts)
        r5 = (
            f"5. Every post is at most {MAX_POST_CHARS} characters. Count every URL as "
            f"{URL_CHARS} characters."
        )
        r6 = f"6. The thread has {lo} to {hi} posts."
    elif fmt.shape == SHAPE_SINGLE:
        r5 = (
            f"5. This is a SINGLE post of at most {MAX_POST_CHARS} characters (a URL counts as "
            f"{URL_CHARS}); rules 2 and 3 apply to that one post."
        )
        r6 = '6. "thread" holds exactly one string: the whole post.'
    else:
        r5 = (
            f"5. This is ONE long-form post of at most {fmt.max_chars} characters (a URL "
            f"counts as {URL_CHARS}), written as short sections separated by blank lines; "
            "rules 2 and 3 apply to that one post."
        )
        r6 = '6. "thread" holds exactly one string: the whole long post.'
    n = 1 if fmt is None else fmt.visuals
    if n == 0:
        r9 = (
            '9. This draft carries NO visual: "chart" and "table" are both null and '
            '"visuals" is absent. "suggested_visual" says what a picture could have shown.'
        )
    elif n == 1:
        r9 = (
            '9. Every draft carries exactly one visual, a "chart" or a "table" (null for the '
            "other); a draft with neither is discarded."
        )
    else:
        r9 = (
            f'9. This draft carries {n} visuals: the first is a "chart" or a "table" (null for '
            'the other) and each further one is a chart in "visuals". A draft with fewer is '
            "discarded."
        )
    return r5, r6, r9


def hard_rules(fmt: Format | None = None) -> str:
    """The HARD RULES block for a format; hard_rules(None) is the pre-phase-four text."""
    r5, r6, r9 = _shape_rules(fmt)
    where = "the first post" if fmt is None or fmt.is_thread else "the post"
    return f"""HARD RULES. A draft that breaks any of these is discarded automatically.
1. No medical advice and no treatment recommendations. Describe evidence; never tell
   anyone what they or their doctor should do.
1b. No investment advice. Never tell anyone to buy, sell, hold, short, or avoid a stock,
   never give a price target, never promise or predict a return. Describe what a result
   means for a company's thesis and the risks; the reader decides.
2. The primary source URL must appear verbatim in the last thread post.
3. If the source is a preprint (bioRxiv / medRxiv), the word "{PREPRINT_LABEL}" must appear in
   the first thread post.
4. Every number you write must appear verbatim in the source abstract or title. Do not
   round, convert units, compute differences or percentages, or infer sample sizes.
   If a number you want is not in the abstract, leave it out.
{r5}
{r6}
7. Every post must contain an interpretation, not just a restatement (see voice guide).
8. Any claim that goes beyond what the abstract states goes into claims_to_verify with an
   honest confidence level.
{r9}
   "chart" is a bar chart that code renders and
   attaches to {where}: give it when the source states two or more comparable
   numbers (arms, endpoints, cohorts) and copy each value exactly as written. Every
   number in the chart is checked against the source like rule 4; one miss and the draft
   is sent back to you. "suggested_visual" is a one-line description of the visual for
   the human reviewer.
10. "table" is the visual when the source has no numbers to chart: a small comparison
   (2-8 rows, 2-5 columns; the first column names the company, asset or trial) such as a
   competitor landscape, a set of upcoming catalysts or the deal terms side by side.
   Unlike a chart its cells MAY come from your own knowledge: every cell is fact-checked
   on the web before the table is drawn, a cell that cannot be tied to a primary source
   is blanked, and one contradicted cell blocks the table. So keep cells short, factual
   and checkable (a phase, a date, a mechanism, a ticker, a number), never an opinion.
   Use "suggested_visual" to say what the table shows.
11. Mentions and hashtags. Write an account whose X handle you are given (the journal or
   society that published the source, the company whose release it is, a regulator) as its
   @handle the first time a post names it, e.g. "just published in @JCO_ASCO". Never invent
   a handle: an account not in the list is written by name. Write every formal drug name
   (the generic or brand name, e.g. #Trastuzumab Deruxtecan, #cilta-cel) and every named
   trial (#DESTINY-Lung02, #KEYNOTE-189) as a hashtag, exactly as the source spells it,
   each time it appears. No other hashtags.
"""


HARD_RULES = hard_rules(None)


@lru_cache(maxsize=1)
def load_voice_guide(path: Path = VOICE_PATH) -> str:
    return path.read_text(encoding="utf-8")


def is_preprint(source: str | None) -> bool:
    """Step 1 names preprint sources like 'biorxiv_cancer_biology' / 'medrxiv_oncology'."""
    name = (source or "").strip().lower()
    return any(p in name for p in PREPRINT_SOURCES)


def build_system_prompt(examples_block: str | None = None, fmt: Format | None = None) -> str:
    """System prompt: voice guide, then (optionally) recent human edits, then the hard rules.

    With examples_block=None the output is byte-identical to the pre-step-7 prompt. The
    examples go AFTER the voice guide and BEFORE HARD_RULES and the schema, so the hard rules
    are the last thing the model reads and no example can relax them. `fmt` (phase four)
    rewrites the shape rules; None keeps the thread physics.
    """
    examples = f"{examples_block.rstrip()}\n\n" if examples_block else ""
    return (
        "You draft posts for an X account on the business and investing side of "
        "immuno-oncology biotech, written as a PhD-level immuno-oncology analyst at a hedge "
        "fund would write them. A human reviews and edits every draft before anything is "
        "published; nothing you write is posted automatically.\n\n"
        "Follow the voice guide exactly.\n\n"
        "=== VOICE GUIDE ===\n"
        f"{load_voice_guide()}\n"
        "=== END VOICE GUIDE ===\n\n"
        f"{examples}"
        f"{hard_rules(fmt)}\n"
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
    handles: list[Handle] | None = None,
) -> str:
    """`handles` (rule 11) are the accounts this story may mention, listed after the
    abstract; with none the prompt is unchanged."""
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
    if handles:
        parts.append(handles_block(handles))
    parts.append("")
    parts.append("Draft the thread and its visual now. Output JSON only.")
    return "\n".join(parts)


@dataclass
class ClaimProblem:
    """A claim the fact-checker (step 2b) could not stand behind, handed to a revision."""

    claim: str
    verdict: str  # 'contradicted' or 'unverified'
    note: str = ""
    quote: str = ""
    source_url: str = ""


def build_revision_user_prompt(
    *,
    base_user_prompt: str,
    current: dict[str, Any],
    instructions: str | None,
    claim_problems: list[ClaimProblem] | None = None,
    cell_problems: list[ClaimProblem] | None = None,
) -> str:
    """The user prompt for a revision round: the original brief, then the draft as it stands,
    the editor's instructions, any claims the fact-checker contradicted and any table cells
    it contradicted (`cell_problems`, each claim worded as `verify/tables.py:cell_claim`).
    The model is told to change only what those ask for and to keep everything else as
    written."""
    parts = [
        base_user_prompt.replace(
            "Draft the thread and its visual now. Output JSON only.",
            "This item was already drafted. You are now REVISING that draft.",
        ),
        "",
        "CURRENT DRAFT (JSON):",
        json.dumps(current, indent=2, ensure_ascii=False),
        "",
    ]
    if instructions:
        parts += [
            "EDITOR INSTRUCTIONS (a human reviewed the draft; do exactly this):",
            instructions,
            "",
        ]
    contradicted = [c for c in (claim_problems or []) if c.verdict == "contradicted"]
    unverified = [c for c in (claim_problems or []) if c.verdict != "contradicted"]
    if contradicted:
        parts.append(
            "FACT-CHECK FAILURES. A fact-checker searched the web and found these claims in the "
            "draft to be WRONG. Correct or remove every one of them, using only the facts in the "
            "fact-checker's note and quote or in the abstract; do not replace a wrong claim with "
            "another guess:"
        )
        parts += [_format_claim_problem(c) for c in contradicted]
        parts.append("")
    if unverified:
        parts.append(
            "UNVERIFIED CLAIMS. The fact-checker could not confirm these. Keep them only if the "
            "abstract supports them; otherwise soften them to what the abstract says or drop them:"
        )
        parts += [_format_claim_problem(c) for c in unverified]
        parts.append("")
    if cell_problems:
        parts.append(
            "TABLE CELL FAILURES. The fact-checker searched the web and found these cells of "
            "the draft's table to be WRONG (each is given as 'row label, column: cell'). Fix "
            "each one in the table: correct the cell using only the facts in the "
            "fact-checker's note and quote, or clear it to an empty string, or remove its "
            "row; do not replace it with another guess. Keep every other cell, header and "
            "the title exactly as they are (a cell's verdict is kept only when its row "
            "label, column and text are unchanged):"
        )
        parts += [_format_claim_problem(c) for c in cell_problems]
        parts.append("")
    parts.append(
        "Rewrite the draft applying the instructions and fixes above. Keep everything the "
        "instructions do not touch as close to the current draft as possible (same angle, same "
        "structure, same wording where it still fits). Every hard rule still applies. Update "
        "claims_to_verify so it lists only claims that remain in the revised text, keeping the "
        "exact wording of every claim that still holds (a fact-checker's verdict is kept only "
        "for a claim whose text is unchanged). "
        "Output the full JSON object only."
    )
    return "\n".join(parts)


def _format_claim_problem(c: ClaimProblem) -> str:
    line = f"- CLAIM: {c.claim}\n  VERDICT: {c.verdict}"
    if c.note:
        line += f"\n  NOTE: {c.note}"
    if c.quote:
        line += f'\n  SOURCE SAYS: "{c.quote}"'
    if c.source_url:
        line += f"\n  SOURCE URL: {c.source_url}"
    return line


def build_prompt(
    *,
    title: str,
    abstract: str,
    url: str,
    source: str,
    published_at: str | None = None,
    suggested_angle: str | None = None,
    rationale: str | None = None,
    examples_block: str | None = None,
    fmt: Format | None = None,
    handles: list[Handle] | None = None,
) -> tuple[str, str]:
    """Return (system_prompt, user_prompt). examples_block (step 7) goes into the system
    prompt; fmt (step 9 phase four) rewrites its shape rules; handles (rule 11) are listed
    in the user prompt."""
    return build_system_prompt(examples_block, fmt), build_user_prompt(
        title=title,
        abstract=abstract,
        url=url,
        source=source,
        published_at=published_at,
        suggested_angle=suggested_angle,
        rationale=rationale,
        handles=handles,
    )
