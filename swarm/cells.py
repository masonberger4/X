"""Pure helpers over cell text: the per-post hard rules, near-twin removal, and the
single-elimination tournament. No DB, no network; the judge is a callable."""

from __future__ import annotations

import difflib
from collections.abc import Callable

from draft.drafter import _ADVICE_RE, _INVEST_RE, _number_in_source, numbers_in
from draft.prompt import PREPRINT_LABEL
from draft.schema import MAX_POST_CHARS, tweet_length
from swarm.genome import CLOSER, HOOK


def cell_problems(
    text: str, *, source_text: str, url: str, slot: str, is_preprint: bool
) -> list[str]:
    """Why one candidate post is unusable. Empty means it may enter the tournament. The same
    rules draft.drafter.check_hard_rules applies to a thread, applied to one post."""
    problems: list[str] = []
    t = text.strip()
    if not t:
        return ["empty"]
    n = tweet_length(t)
    if n > MAX_POST_CHARS:
        problems.append(f"{n} chars (> {MAX_POST_CHARS})")
    if m := _ADVICE_RE.search(t):
        problems.append(f"reads as medical advice: {m.group(0)!r}")
    if m := _INVEST_RE.search(t):
        problems.append(f"reads as investment advice: {m.group(0)!r}")
    missing = [num for num in numbers_in(t) if not _number_in_source(num, source_text)]
    if missing:
        problems.append("numbers not in the source: " + ", ".join(dict.fromkeys(missing)))
    if slot == CLOSER and url not in t:
        problems.append("closer is missing the primary source URL")
    if slot == HOOK and is_preprint and PREPRINT_LABEL not in t.lower():
        problems.append("preprint not labelled in the hook")
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
