"""Pure helpers over cell text: the per-post hard rules, near-twin removal, and the
single-elimination tournament. No DB, no network; the judge is a callable."""

from __future__ import annotations

import difflib
from collections.abc import Callable

from draft.drafter import (
    _ADVICE_RE,
    _INVEST_RE,
    _number_in_source,
    known_company_names,
    numbers_in,
)
from draft.hook import hook_problems
from draft.prompt import PREPRINT_LABEL
from draft.schema import MAX_POST_CHARS, tweet_length
from draft.tags import Handle, tag_problems
from swarm.genome import CLOSER, HOOK


def cell_problems(
    text: str,
    *,
    source_text: str,
    url: str,
    slot: str,
    is_preprint: bool,
    max_chars: int = MAX_POST_CHARS,
    needs_url: bool | None = None,
    needs_preprint: bool | None = None,
    handles: list[Handle] | None = None,
    is_hook: bool | None = None,
) -> list[str]:
    """Why one candidate post is unusable. Empty means it may enter the tournament. The same
    rules draft.drafter.check_hard_rules applies to a thread, applied to one post.
    `max_chars` is the cell's limit (a long post's section, phase four); `needs_url` /
    `needs_preprint` override the slot-name defaults (the closer carries the URL, the hook
    the preprint label) for a single-post format where one cell must do both. `handles`
    are the accounts the story may mention (rule 11: a name without its @handle fails; a
    trial or drug name without its # always fails). `is_hook` overrides the slot-name
    default for rule 12 (draft.hook): a hook cell is a link-free one-claim opener, unless
    it is also the cell carrying the URL (a single or long post)."""
    problems: list[str] = []
    t = text.strip()
    if not t:
        return ["empty"]
    n = tweet_length(t)
    if n > max_chars:
        problems.append(f"{n} chars (> {max_chars})")
    if m := _ADVICE_RE.search(t):
        problems.append(f"reads as medical advice: {m.group(0)!r}")
    if m := _INVEST_RE.search(t):
        problems.append(f"reads as investment advice: {m.group(0)!r}")
    missing = [num for num in numbers_in(t) if not _number_in_source(num, source_text)]
    if missing:
        problems.append("numbers not in the source: " + ", ".join(dict.fromkeys(missing)))
    if needs_url is None:
        needs_url = slot == CLOSER
    if needs_preprint is None:
        needs_preprint = slot == HOOK and is_preprint
    if needs_url and url not in t:
        problems.append(f"{slot} is missing the primary source URL")
    if needs_preprint and PREPRINT_LABEL not in t.lower():
        problems.append(f"preprint not labelled in the {slot}")
    if is_hook is None:
        is_hook = slot == HOOK
    if is_hook:
        problems += hook_problems(t, url=url, carries_url=needs_url)
    problems += tag_problems(t, handles, known_company_names())
    return problems


def similarity(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, a.lower().split(), b.lower().split()).ratio()


def dedupe(candidates: list[str], max_similarity: float) -> list[str]:
    """Keep the first of any group of near twins so the tournament compares real
    alternatives. Order is preserved."""
    kept: list[str] = []
    for c in candidates:
        if any(similarity(c, k) >= max_similarity for k in kept):
            continue
        kept.append(c)
    return kept


Judge = Callable[[str, str], str]


def tournament(candidates: list[str], judge: Judge) -> tuple[str, list[dict]]:
    """Single elimination. judge(a, b) returns the winning text. The A/B presentation order is
    swapped on alternate matches so a position-biased judge does not decide every round the
    same way. Returns (winner, match log)."""
    if not candidates:
        raise ValueError("no candidates")
    log: list[dict] = []
    pool = list(candidates)
    match = 0
    while len(pool) > 1:
        nxt: list[str] = []
        for i in range(0, len(pool) - 1, 2):
            a, b = pool[i], pool[i + 1]
            first, second = (a, b) if match % 2 == 0 else (b, a)
            winner = judge(first, second)
            if winner not in (a, b):
                winner = a
            log.append({"a": first, "b": second, "winner": winner})
            nxt.append(winner)
            match += 1
        if len(pool) % 2:
            nxt.append(pool[-1])  # bye
        pool = nxt
    return pool[0], log
