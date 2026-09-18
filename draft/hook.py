"""The opening post's own rules and the link ban (pure: no DB, no network, no clock).

X ranks a thread by what its FIRST post does in the first minutes: a reader who cannot
parse the claim never reaches post 2. So the opener is not a summary of the thread, it is
the thread's single claim, and it carries no position marker ("1/6").

No post carries a link of any kind. An outbound link costs the post reach, and posting a
URL is billed as an extra request through the X API, so the source is named in words (the
journal, the company, the meeting) and never linked. `link_problems` is applied by
draft.drafter.check_hard_rules to every post and by swarm.cells.cell_problems to every
cell; `hook_problems` adds the opener's own rules on top.
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


_URL = re.compile(r"https?://\S+")
# A bare domain written without a scheme is still a link to X's parser.
_BARE_DOMAIN = re.compile(
    r"(?<![\w@./-])(?:www\.\S+|[a-z0-9-]+(?:\.[a-z0-9-]+)*\.[a-z]{2,}/\S*)",
    re.I,
)


def link_problems(text: str) -> list[str]:
    """Why this post would be discarded for carrying a link. No post carries one: a URL
    costs the post reach and an extra billed request through the X API."""
    t = text.strip()
    found = _URL.search(t) or _BARE_DOMAIN.search(t)
    if not found:
        return []
    return [
        f"contains a link ({found.group(0)}); no post carries a URL of any kind, "
        "name the source in words instead"
    ]


def hook_problems(text: str, *, max_chars: int | None = HOOK_MAX_CHARS) -> list[str]:
    """Why this opener would be discarded. `max_chars` is the opener's cap, or None for a
    single or long post, whose body is capped by its own format instead. The link ban is
    `link_problems`, applied to every post."""
    problems: list[str] = []
    t = text.strip()
    for pattern, label in _MARKERS:
        if pattern.search(t):
            problems.append(f"first post contains {label}; the client numbers the thread")
    n = tweet_length(t)
    if max_chars is not None and n > max_chars:
        problems.append(
            f"first post is {n} chars (> {max_chars}); the opener is one claim, not a summary"
        )
    return problems


def hook_rule(*, capped: bool = True) -> str:
    """The prompt wording of the rule, so the model is told exactly what is enforced.
    `capped` is False for a single or long post, whose length is the format's, not the
    hook cap."""
    if not capped:
        return (
            "12. Open with the claim. The first sentence states the single most important\n"
            "   finding or consequence in words a specialist could answer or argue with; it\n"
            '   never says "thread", never numbers itself ("1/6") and carries no emoji.'
        )
    return (
        f"12. The first post is the hook, and it decides whether anyone reads the rest: at\n"
        f"   most {HOOK_MAX_CHARS} characters (a URL counts as {URL_CHARS}, but see rule 2: "
        "there are none),\n"
        "   ONE claim a specialist could answer or argue with, no thread position marker\n"
        '   ("1/6"), no "thread", no emoji. It is the sharpest line of the draft, not a\n'
        "   summary of it: the setup and the caveats come in the later posts."
    )


# Lead-ins that exist only to introduce a link ("Source: <URL>", "Paper here:"). When the
# URL is removed they are left dangling, so the strip takes them with it.
_LEAD_IN = re.compile(
    r"(?:\b(?:full\s+)?(?:source|sources|paper|study|link|details|abstract|release|"
    r"read\s+it|more|results|preprint)\b[^.\n]{0,20})$",
    re.I,
)
_TRAILING_JUNK = " \t\r\n([{<\"'«—–-:;,."


def strip_links(text: str) -> str | None:
    """Take every link out of a post, along with a lead-in that only introduced one.

    The pure half of run_unlink.py, the one-off pass over drafts written while posts still
    carried the source URL. Returns None when the post holds no link, or when nothing but
    the link would be left, which is the caller's signal to drop the post.
    """
    if not link_problems(text):
        return None
    ended = _ends_with_link(text)
    body = _BARE_DOMAIN.sub(" ", _URL.sub(" ", text))
    body = body.replace("()", " ").replace("[]", " ")
    lines = [re.sub(r"[ \t]{2,}", " ", ln).rstrip() for ln in body.splitlines()]
    body = "\n".join(lines).strip(_TRAILING_JUNK)
    # Only a link that ended the post can have left a lead-in dangling; one in the middle
    # of a sentence is followed by the writer's own words, which are never dropped.
    if ended:
        body = _LEAD_IN.sub("", body).strip(_TRAILING_JUNK)
    if not body:
        return None
    if body[-1].isalnum() or body[-1] in ")”":
        body += "."
    return body


def _ends_with_link(text: str) -> bool:
    """True when the post's last words are a link (bare, bracketed or punctuated)."""
    tail = text.rstrip().rstrip(")]>»\"'.,;:")
    for pattern in (_URL, _BARE_DOMAIN):
        matches = list(pattern.finditer(tail))
        if matches and matches[-1].end() == len(tail):
            return True
    return False
