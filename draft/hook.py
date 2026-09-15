"""The opening post's own rules (pure: no DB, no network, no clock).

X ranks a thread by what its FIRST post does in the first minutes: a reader who cannot
parse the claim, or who is sent off-platform by a link, never reaches post 2. So the
opener is not a summary of the thread, it is the thread's single claim, and it carries
neither the source URL nor a position marker ("1/6", "a thread").

`hook_problems` is applied by draft.drafter.check_hard_rules to the first post of a
thread and by swarm.cells.cell_problems to a hook cell, so the single drafter and the
swarm are held to the same opener.
"""

from __future__ import annotations

import re

from draft.schema import URL_CHARS, tweet_length

# A one-line claim, not a paragraph. Threads whose opener runs past this read as a summary
# and lose the reader before the claim lands; 280 is still the hard ceiling above it.
HOOK_MAX_CHARS = 220

# Position markers and thread throat-clearing. The client already shows thread position,
# so "1/6" only spends characters and marks the post as a broadcast.
_MARKERS = (
    (re.compile(r"(?<!\d)\d{1,2}\s*/\s*(?:\d{1,2}|n)(?!\d)"), "a thread position marker"),
    (re.compile(r"\U0001f9f5"), "the thread emoji"),
    (
        re.compile(r"\b(?:a |the )?thread\b(?:\s+(?:below|follows|incoming))?", re.I),
        "the word 'thread'",
    ),
    (re.compile(r"\b(?:more|details|full breakdown)\s+below\b", re.I), "'more below'"),
)


_OTHER_URL = re.compile(r"https?://\S+")


def hook_problems(text: str, *, url: str = "", carries_url: bool = False) -> list[str]:
    """Why this opener would be discarded. `carries_url` is True only when the opener is
    also the post that must hold the source URL (a single or long post), which then exempts
    it from the link rule and from the length cap."""
    problems: list[str] = []
    t = text.strip()
    for pattern, label in _MARKERS:
        if pattern.search(t):
            problems.append(f"first post contains {label}; the client numbers the thread")
    if not carries_url:
        if url and url in t:
            problems.append(
                "first post contains the source URL; an outbound link in the opening post "
                "is shown to fewer non-followers, so the URL belongs in the last post"
            )
        elif _OTHER_URL.search(t):
            problems.append("first post contains a link; links belong in the last post")
        n = tweet_length(t)
        if n > HOOK_MAX_CHARS:
            problems.append(
                f"first post is {n} chars (> {HOOK_MAX_CHARS}); the opener is one claim, "
                "not a summary"
            )
    return problems


def hook_rule(*, carries_url: bool) -> str:
    """The prompt wording of the rule, so the model is told exactly what is enforced."""
    if carries_url:
        return (
            "12. Open with the claim. The first sentence states the single most important\n"
            "   finding or consequence in words a specialist could answer or argue with; it\n"
            '   never says "thread", never numbers itself ("1/6") and carries no emoji.'
        )
    return (
        f"12. The first post is the hook, and it decides whether anyone reads the rest: at\n"
        f"   most {HOOK_MAX_CHARS} characters (a URL counts as {URL_CHARS}), ONE claim a "
        "specialist could\n"
        "   answer or argue with, and NO link of any kind, no thread position marker\n"
        '   ("1/6"), no "thread", no emoji. It is the sharpest line of the draft, not a\n'
        "   summary of it: the setup, the caveats and the source URL come in the later posts."
    )
