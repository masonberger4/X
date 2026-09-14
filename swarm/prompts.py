"""Prompt builders for the swarm. Pure: strings in, strings out.

A cell prompt is deliberately narrow. The model sees the source brief, ONE slot's rule, the
cells already chosen for the earlier slots, and the hard rules that apply to a single post.
It never sees the whole voice guide or the whole thread; the arrangement, not any one
call, is what carries the analyst voice. The assembly prompt is the exception: it gets the
full step 2 system prompt (voice guide, hard rules, schema) because it must produce the
step 2 JSON object, but it is told to keep the chosen cells as they are.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from draft.prompt import PREPRINT_LABEL, is_preprint
from draft.schema import MAX_POST_CHARS, SHAPE_LONG, SHAPE_SINGLE, URL_CHARS, Format
from draft.tags import Handle, handles_block
from swarm.genome import CLOSER, HOOK, Genome, Slot


@dataclass(frozen=True)
class Brief:
    """What every cell is allowed to know about the story."""

    title: str
    abstract: str
    url: str
    source: str
    published_at: str | None = None
    suggested_angle: str | None = None
    rationale: str | None = None
    handles: tuple[Handle, ...] = ()  # the accounts this story may @-mention (rule 11)

    @property
    def preprint(self) -> bool:
        return is_preprint(self.source)

    @property
    def source_text(self) -> str:
        return f"{self.title}\n{self.abstract}"


PERSONA = (
    "You write ONE post of an X thread for an account on the business and investing side "
    "of immuno-oncology biotech, in the voice of a PhD-level immuno-oncology analyst at a "
    "hedge fund: precise about mechanism and trial design, explicit about what a result "
    "does to a company's thesis, never a stock tip."
)


def cell_rules(max_chars: int = MAX_POST_CHARS) -> str:
    """The per-cell rules; `max_chars` is 280, or a long post's section limit."""
    unit = "post" if max_chars == MAX_POST_CHARS else "section of a long post"
    return f"""RULES for this one {unit} (one that breaks a rule is discarded by code):
- At most {max_chars} characters; a URL counts as {URL_CHARS}. Aim under {max_chars - 30}.
- No medical advice or treatment recommendations. No investment advice: never buy, sell,
  hold, short, a price target or a promised return. Describe; the reader decides.
- Every number must appear verbatim in the source title or abstract. Do not round,
  convert, subtract or compute. No number in the source means no number in the post.
- An interpretation, not a restatement: say what it means, what to watch, what is overhyped.
- Write an account whose handle the brief lists as @handle when you name it; never invent
  a handle. Write every formal drug name (#Trastuzumab Deruxtecan, #cilta-cel) and every
  named trial (#KEYNOTE-189, #DESTINY-Lung02) as a hashtag, spelled as the source spells it.
- Otherwise plain text: no other hashtags, no emoji, no "1/", no quotation of the whole abstract."""


CELL_RULES = f"""RULES for this one post (a post that breaks one is discarded by code):
- At most {MAX_POST_CHARS} characters; a URL counts as {URL_CHARS}. Aim under {MAX_POST_CHARS - 30}.
- No medical advice or treatment recommendations. No investment advice: never buy, sell,
  hold, short, a price target or a promised return. Describe; the reader decides.
- Every number must appear verbatim in the source title or abstract. Do not round,
  convert, subtract or compute. No number in the source means no number in the post.
- An interpretation, not a restatement: say what it means, what to watch, what is overhyped.
- Write an account whose handle the brief lists as @handle when you name it; never invent
  a handle. Write every formal drug name (#Trastuzumab Deruxtecan, #cilta-cel) and every
  named trial (#KEYNOTE-189, #DESTINY-Lung02) as a hashtag, spelled as the source spells it.
- Otherwise plain text: no other hashtags, no emoji, no "1/", no quotation of the whole abstract."""


def brief_block(brief: Brief) -> str:
    parts = [
        f"SOURCE: {brief.source}" + ("  (THIS IS A PREPRINT)" if brief.preprint else ""),
        f"PRIMARY SOURCE URL: {brief.url}",
        f"PUBLISHED: {brief.published_at or 'unknown'}",
        f"TITLE: {brief.title}",
        "",
        "ABSTRACT:",
        brief.abstract or "(no abstract available; do not invent findings)",
        "",
    ]
    if brief.rationale:
        parts.append(f"SCORER RATIONALE: {brief.rationale}")
    if brief.suggested_angle:
        parts.append(f"SUGGESTED ANGLE: {brief.suggested_angle}")
    if brief.handles:
        parts.append(handles_block(list(brief.handles)))
    return "\n".join(parts)


SINGLE_SLOT = Slot(
    "single",
    "Write the whole post: the single most important finding or event, what it does to the "
    "company's thesis, and the one caveat, in one tight post. Name the company, asset or "
    "trial. No throat-clearing, no emoji.",
)


def _slot_extras(slot: Slot, brief: Brief) -> str:
    extras = []
    if (slot.name == HOOK or slot.name == SINGLE_SLOT.name) and brief.preprint:
        extras.append(f'The word "{PREPRINT_LABEL}" must appear in this post.')
    if slot.name == CLOSER or slot.name == SINGLE_SLOT.name:
        extras.append(f"This post must contain the URL exactly as given: {brief.url}")
    return "\n".join(extras)


def _context_block(chosen: dict[str, str]) -> str:
    if not chosen:
        return "POSTS ALREADY CHOSEN BEFORE YOURS: none (yours opens the thread)."
    lines = ["POSTS ALREADY CHOSEN BEFORE YOURS (do not repeat them; continue from them):"]
    for name, text in chosen.items():
        lines.append(f"[{name}] {text}")
    return "\n".join(lines)


def cell_system_prompt(max_chars: int = MAX_POST_CHARS) -> str:
    return (
        f"{PERSONA}\n\n{cell_rules(max_chars)}\n\n"
        "Answer with the post text only: no JSON, no quotes, no preamble."
    )


def propose_prompt(brief: Brief, slot: Slot, chosen: dict[str, str]) -> str:
    """Layer 1: write the slot's post from the brief alone."""
    parts = [
        brief_block(brief),
        "",
        _context_block(chosen),
        "",
        f"YOUR SLOT: {slot.name}",
        f"YOUR JOB: {slot.rule}",
    ]
    extra = _slot_extras(slot, brief)
    if extra:
        parts.append(extra)
    parts += ["", "Write the post now."]
    return "\n".join(parts)


def synthesise_prompt(brief: Brief, slot: Slot, chosen: dict[str, str], previous: list[str]) -> str:
    """Layer 2 and up (Mixture-of-Agents): every earlier layer's candidates are shown and the
    model writes a better one, free to merge the strongest parts."""
    parts = [
        brief_block(brief),
        "",
        _context_block(chosen),
        "",
        f"YOUR SLOT: {slot.name}",
        f"YOUR JOB: {slot.rule}",
    ]
    extra = _slot_extras(slot, brief)
    if extra:
        parts.append(extra)
    parts += ["", "CANDIDATES FROM THE PREVIOUS ROUND (other writers, same slot):"]
    parts += [f"{i + 1}. {c}" for i, c in enumerate(previous)]
    parts += [
        "",
        "Write a better post for this slot. Keep what is strongest in the candidates, drop "
        "what is weak, and do not add any fact or number that is not in the source. One "
        "post only.",
    ]
    return "\n".join(parts)


JUDGE_SYSTEM = (
    "You are a strict editor for an X account written by a hedge-fund immuno-oncology "
    "analyst. You compare two candidate posts for the same slot of a thread and pick the "
    "one a buy-side reader would rather read: sharper interpretation, more precise, less "
    "hype, better continuation of the posts before it. Answer with JSON only: "
    '{"winner": "A" or "B", "reason": "<one sentence>"}.'
)


def judge_prompt(brief: Brief, slot: Slot, chosen: dict[str, str], a: str, b: str) -> str:
    return "\n".join(
        [
            brief_block(brief),
            "",
            _context_block(chosen),
            "",
            f"SLOT: {slot.name}",
            f"THE SLOT'S JOB: {slot.rule}",
            "",
            f"A: {a}",
            "",
            f"B: {b}",
            "",
            "Which is better for this slot? JSON only.",
        ]
    )


THREAD_JUDGE_SYSTEM = (
    "You are a strict editor for an X account written by a hedge-fund immuno-oncology "
    "analyst. You compare two complete candidate threads about the same story and pick "
    "the one that would earn more thoughtful engagement from biotech investors: clearer "
    "hook, more precise science, sharper read on the company's thesis, honest caveats, "
    "no hype, no advice. Answer with JSON only: "
    '{"winner": "A" or "B", "reason": "<one sentence>"}.'
)


def thread_judge_prompt(brief: Brief, a: list[str], b: list[str]) -> str:
    def fmt(label: str, thread: list[str]) -> list[str]:
        return [f"THREAD {label}:"] + [f"  {i + 1}. {p}" for i, p in enumerate(thread)] + [""]

    return "\n".join(
        [brief_block(brief), ""]
        + fmt("A", a)
        + fmt("B", b)
        + ["Which thread is better? JSON only."]
    )


def _visual_sentence(fmt: Format | None) -> str:
    n = 1 if fmt is None else fmt.visuals
    if n == 0:
        return 'This format carries no visual: "chart" and "table" are both null.'
    if n == 1:
        return (
            "Then supply the visual (a chart only from numbers written verbatim in the "
            "source, otherwise a table)"
        )
    return (
        f'Then supply {n} visuals: the first as "chart" (numbers verbatim from the source) '
        'or "table", and each further one as a chart in "visuals" (numbers verbatim)'
    )


def assemble_prompt(
    brief: Brief, genome: Genome, cells: dict[str, str], fmt: Format | None = None
) -> str:
    """The user prompt for the assembly call. The system prompt is step 2's
    (draft.prompt.build_system_prompt): voice guide, hard rules, output schema. `fmt`
    (phase four) says whether the cells are a thread's posts, one single post or the
    sections of one long post, and how many visuals to give."""
    shape = "thread" if fmt is None else fmt.shape
    if shape == SHAPE_SINGLE:
        what = "This single post was written by a team of writers and chosen by editors:"
    elif shape == SHAPE_LONG:
        what = (
            "This long-form post was written one section per slot by a team of writers and "
            "chosen by editors. The sections, in order, are:"
        )
    else:
        what = (
            "This thread was written one post per slot by a team of writers and chosen by "
            "editors. The posts, in order, are:"
        )
    parts = [brief_block(brief), "", what, ""]
    names = [slot.name for slot in genome.slots] if shape != SHAPE_SINGLE else [SINGLE_SLOT.name]
    for name in names:
        text = cells.get(name, "")
        if text:
            parts.append(f"[{name}] {text}")
    if shape == SHAPE_SINGLE:
        how = (
            'Assemble the draft JSON. "thread" holds exactly one string: this post, kept as '
            "written (you may fix grammar or trim it under the limit; do not rewrite it). "
        )
    elif shape == SHAPE_LONG:
        how = (
            'Assemble the draft JSON. "thread" holds exactly one string: these sections IN '
            "THIS ORDER joined by blank lines, kept as written (fix grammar, trim a section "
            "that runs over; do not rewrite, reorder or add sections). The whole post must "
            f"stay under {fmt.max_chars if fmt else MAX_POST_CHARS} characters. "
        )
    else:
        lo, hi = (fmt.min_posts, fmt.max_posts) if fmt else (3, 6)
        how = (
            "Assemble the draft JSON. The thread is these posts IN THIS ORDER, kept as "
            "written: you may fix grammar, trim a post that runs over the limit, or drop one "
            f"post if the thread would otherwise exceed {hi}, but do not rewrite, reorder or "
            f"add posts (the thread has {lo} to {hi} posts). "
        )
    parts += [
        "",
        how
        + _visual_sentence(fmt)
        + ", suggested_visual, why_it_matters and claims_to_verify for every claim in "
        "these posts that goes beyond the abstract. Output the JSON object only.",
    ]
    return "\n".join(parts)


def parse_winner(text: str) -> str | None:
    """'A' or 'B' from a judge answer; None when the answer names neither exactly once."""
    text = text.strip()
    try:
        start, end = text.index("{"), text.rindex("}") + 1
        data = json.loads(text[start:end])
        w = str(data.get("winner", "")).strip().upper()
        if w in ("A", "B"):
            return w
    except (ValueError, AttributeError, TypeError):
        pass
    upper = text.upper()
    hits = [w for w in ("A", "B") if f"WINNER: {w}" in upper or upper.strip() == w]
    return hits[0] if len(hits) == 1 else None
