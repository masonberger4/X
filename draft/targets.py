"""Where a post cites a price target (pure: no DB, no network).

An analyst's target belongs in a post only with what it rests on and whether the post's
catalyst is in it. The studio's sessions can research that (studio/qa.py:check_price_targets
holds each citation to it); a drafter thread cannot, so the drafter cites none
(drafter.check_hard_rules, swarm/cells.py:cell_problems). Both find the citations here.

"Targets PD-1", "a $5B target market", "the target population" and "a revenue target of
$500M" are not price targets: a citation is the phrase (price target, target price, the
consensus or average target) or a per-share figure written next to the word target, PT or
fair value.
"""

from __future__ import annotations

import re

# A per-share figure ($20, $12.40, ~$15), never a market size or a sales figure ($5B, $500M,
# $410-420M, $2 billion).
_SCALE = r"\s*(?:[bmk]n?\b|billion|million|thousand|trillion)"
_SHARE_FIG = (
    r"\$\s?(\d{1,4}(?:\.\d{1,2})?)(?![\d.,]*\d)(?!" + _SCALE + r")"
    r"(?!\s*[-\u2013]\s*\$?\d[\d.,]*" + _SCALE + r")"
)
# Words that may stand between a figure and the word target: "$12.40 consensus target".
_TARGET_ADJ = (
    r"(?:(?:consensus|average|mean|median|street(?:-high|-low)?|new|old|previous|prior|"
    r"raised|lowered|reduced|analyst|analysts['\u2019]|12-month|one-year|high|low|highest|"
    r"lowest|bull|bear|base|blended)\s+){0,3}"
)
_TARGET_PHRASE_RE = re.compile(
    r"\bprice[-\s]+targets?\b|\btarget\s+prices?\b|"
    r"\b(?:(?:analyst|analysts['\u2019]?|consensus|street|average|mean|median|highest|"
    r"lowest)\s+){1,2}(?:price\s+)?targets?\b",
    re.I,
)
# The word a figure is cited as: a target, a target price or a fair value (estimate).
_TARGET_WORD = r"(?:(?:price\s+)?targets?(?:\s+prices?)?|fair\s+values?(?:\s+estimates?)?)"
# "a $15 target", "$12.40 consensus target", "$38 price target", "$30 fair value".
_FIG_BEFORE_RE = re.compile(_SHARE_FIG + r"\s+" + _TARGET_ADJ + _TARGET_WORD + r"\b", re.I)
# "target to $20", "target of $38", "price target (~$12.40", "fair value near $30".
_FIG_AFTER_RE = re.compile(
    r"\b" + _TARGET_WORD + r"\s+(?:(?:to|of|at|from|is|was|near|around|above|below|"
    r"sits\s+at|stands\s+at)\s+)?(?:about\s+|roughly\s+|around\s+)?[(~]?\s*~?" + _SHARE_FIG,
    re.I,
)
# What follows a cited figure with the move: "to $20 from $9", "$38 (cut from $45)", and
# a second firm whose target goes unsaid: ", and Wells Fargo to $18".
_FIG_CHAIN_RE = re.compile(
    r"\s*[(,]?\s*(?:(?:cut|raised|lowered|reduced|trimmed|up|down|moved)\s+)?"
    r"(?:from|to|vs\.?|versus)\s+~?"
    + _SHARE_FIG
    + r"|,?\s*(?:and|while)\s+(?:[A-Z][\w.&'\u2019-]*\s+){1,3}(?:(?:raised|cut|lowered|"
    r"moved|went)\s+)?(?:to|at)\s+~?" + _SHARE_FIG
)
# "PT $25", "PT to $25", "$25 PT" (the abbreviation only in capitals, only with a figure).
_PT_RE = re.compile(
    r"(?<![A-Za-z])PTs?\s+(?:(?:to|of|at|from|is)\s+)?~?"
    + _SHARE_FIG
    + r"|"
    + _SHARE_FIG
    + r"\s+PTs?(?![A-Za-z])"
)


def target_figures(text: str) -> list[str]:
    """The per-share figures a text gives as price targets, in order, without the $:
    "raised its target to $20 from $9" gives ["20", "9"]. Only figures written next to the
    word target (or PT) count; a share price or a market size elsewhere in the sentence
    does not."""
    found: list[tuple[int, str]] = []
    for rx in (_FIG_BEFORE_RE, _FIG_AFTER_RE, _PT_RE):
        for m in rx.finditer(text):
            i = next(k for k in range(1, (m.lastindex or 0) + 1) if m.group(k) is not None)
            found.append((m.start(i), m.group(i)))
            end = m.end()  # "$38 target (cut from $45)", ", and Guggenheim at $40"
            while chained := _FIG_CHAIN_RE.match(text, end):
                k = next(j for j in range(1, (chained.lastindex or 0) + 1) if chained.group(j))
                found.append((chained.start(k), chained.group(k)))
                end = chained.end()
    return list(dict.fromkeys(fig for _, fig in sorted(found)))


def target_mentions(text: str) -> list[str]:
    """Where a text cites a price target, each as the words that show it ("price target of
    $40", "target to $20", "$15 target"); [] when it cites none. "Targets PD-1", "a $5B
    target market" and "the target population" are not price targets."""
    spans = sorted(
        (m.start(), m.end())
        for rx in (_TARGET_PHRASE_RE, _FIG_BEFORE_RE, _FIG_AFTER_RE, _PT_RE)
        for m in rx.finditer(text)
    )
    merged: list[list[int]] = []
    for start, end in spans:  # overlapping matches are one citation
        if merged and start < merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return list(dict.fromkeys(" ".join(text[s:e].split()) for s, e in merged))


def target_problems(text: str) -> list[str]:
    """The drafter's rule: a thread cites no price target, an analyst's included, since it
    has no room to say what the target rests on."""
    found = target_mentions(text)
    if not found:
        return []
    return [
        f"cites a price target ({found[0]!r}); a thread cannot say what a target rests on, "
        "so it cites none (leave the analysts' targets out)"
    ]
