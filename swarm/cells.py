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
    money_numbers,
    numbers_in,
)
from draft.hook import HOOK_MAX_CHARS, hook_problems, link_problems
from draft.prompt import PREPRINT_LABEL
from draft.schema import MAX_POST_CHARS, tweet_length
from draft.style import style_problems
from draft.tags import Handle, tag_problems
from draft.targets import target_problems
from swarm.genome import HOOK


def cell_problems(
    text: str,
    *,
    source_text: str,
    slot: str,
    is_preprint: bool,
    max_chars: int = MAX_POST_CHARS,
    needs_preprint: bool | None = None,
    handles: list[Handle] | None = None,
    is_hook: bool | None = None,
    hook_capped: bool = True,
) -> list[str]:
    """Why one candidate post is unusable. Empty means it may enter the tournament. The same
    rules draft.drafter.check_hard_rules applies to a thread, applied to one post.
    `max_chars` is the cell's limit (a long post's section, phase four); `needs_preprint`
    overrides the slot-name default (the hook carries the preprint label) for a
    single-post format. No cell carries a link (rule 2, draft.hook.link_problems).
    `handles` are the accounts the story may mention (rule 11: a name without its @handle
    fails; an NCT number or drug name without its # always fails). `is_hook` overrides the
    slot-name default for rule 12 (draft.hook): a hook cell is a one-claim opener, and
    `hook_capped` is False where the hook's 220-character cap does not apply (a single or
    long post's body, capped by its own format instead)."""
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
    else:
        problems += target_problems(t)
    money = money_numbers(t)  # rule 4's exception: the assembly lists them as claims
    missing = [
        num for num in numbers_in(t) if num not in money and not _number_in_source(num, source_text)
    ]
    if missing:
        problems.append("numbers not in the source: " + ", ".join(dict.fromkeys(missing)))
    problems += link_problems(t)
    problems += style_problems(t)
    if needs_preprint is None:
        needs_preprint = slot == HOOK and is_preprint
    if needs_preprint and PREPRINT_LABEL not in t.lower():
        problems.append(f"preprint not labelled in the {slot}")
    if is_hook is None:
        is_hook = slot == HOOK
    if is_hook:
        problems += hook_problems(t, max_chars=HOOK_MAX_CHARS if hook_capped else None)
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


JudgeRound = Callable[[list[tuple[str, str]]], list[str]]


def tournament(
    candidates: list[str], judge: Judge | None = None, *, judge_round: JudgeRound | None = None
) -> tuple[str, list[dict]]:
    """Single elimination. judge(a, b) returns the winning text; `judge_round` instead takes
    every (a, b) match of one round at once (so the engine can judge them side by side) and
    returns the winners in order. The A/B presentation order is swapped on alternate matches
    so a position-biased judge does not decide every round the same way. Returns (winner,
    match log)."""
    if not candidates:
        raise ValueError("no candidates")
    if judge_round is None:
        if judge is None:
            raise ValueError("tournament needs judge or judge_round")
        _judge = judge

        def judge_round(pairs: list[tuple[str, str]]) -> list[str]:
            return [_judge(a, b) for a, b in pairs]

    log: list[dict] = []
    pool = list(candidates)
    match = 0
    while len(pool) > 1:
        pairs: list[tuple[str, str]] = []
        presented: list[tuple[str, str]] = []
        for i in range(0, len(pool) - 1, 2):
            a, b = pool[i], pool[i + 1]
            pairs.append((a, b))
            presented.append((a, b) if match % 2 == 0 else (b, a))
            match += 1
        winners = judge_round(presented)
        nxt: list[str] = []
        for (a, b), (first, second), winner in zip(pairs, presented, winners, strict=True):
            if winner not in (a, b):
                winner = a
            log.append({"a": first, "b": second, "winner": winner})
            nxt.append(winner)
        if len(pool) % 2:
            nxt.append(pool[-1])  # bye
        pool = nxt
    return pool[0], log
