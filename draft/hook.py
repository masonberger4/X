"""The opening post's own rules (pure: no DB, no network, no clock).

X ranks a thread by what its FIRST post does in the first minutes: a reader who cannot
parse the claim, or who is sent off-platform by a link, never reaches post 2. So the
opener is not a summary of the thread, it is the thread's single claim, and it carries
neither the source URL nor a position marker ("1/6", "a thread").

`hook_problems` is applied by draft.drafter.check_hard_rules to the first post of a
thread and by swarm.cells.cell_problems to a hook cell, so the single drafter and the
swarm are held to the same opener.

The same reasoning is why a single or long post is not exempt: it too opens with a
link-free claim, and its source URL goes in a second, threaded post (`link_post_problems`)
instead of in the body.
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

# The link post (a single or long post's second post) holds the URL and nothing more than
# a few words of attribution.
LINK_POST_MAX_CHARS = 120


def hook_problems(
    text: str,
    *,
    url: str = "",
    carries_url: bool = False,
    max_chars: int | None = HOOK_MAX_CHARS,
) -> list[str]:
    """Why this opener would be discarded. `carries_url` is True only for an opener that is
    itself the post holding the source URL (a draft written before the link post, kept
    working), which exempts it from the link rule and from the length cap. `max_chars` is
    the opener's cap, or None for a single or long post, whose body is capped by its own
    format instead."""
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
        if max_chars is not None and n > max_chars:
            problems.append(
                f"first post is {n} chars (> {max_chars}); the opener is one claim, not a summary"
            )
    return problems


def link_post_problems(text: str, *, url: str) -> list[str]:
    """Why the link post of a single or long post would be discarded. It exists only to
    carry the primary source URL out of the body post, so it holds that URL, no other link,
    and little else."""
    problems: list[str] = []
    t = text.strip()
    if url and url not in t:
        problems.append("the link post is missing the primary source URL")
    others = [u for u in _OTHER_URL.findall(t) if not (url and u.startswith(url))]
    if others:
        problems.append(f"the link post contains another link: {others[0]}")
    n = tweet_length(t)
    if n > LINK_POST_MAX_CHARS:
        problems.append(
            f"the link post is {n} chars (> {LINK_POST_MAX_CHARS}); it carries the source "
            "URL and at most a few words of attribution"
        )
    return problems


def hook_rule(*, carries_url: bool, capped: bool = True) -> str:
    """The prompt wording of the rule, so the model is told exactly what is enforced.
    `capped` is False for a single or long post: it carries no link either, but its length
    is the format's, not the hook cap."""
    if carries_url:
        return (
            "12. Open with the claim. The first sentence states the single most important\n"
            "   finding or consequence in words a specialist could answer or argue with; it\n"
            '   never says "thread", never numbers itself ("1/6") and carries no emoji.'
        )
    if not capped:
        return (
            "12. Open with the claim, and carry NO link of any kind in this post: the source\n"
            "   URL goes in the second post (rule 2). The first sentence states the single\n"
            "   most important finding or consequence in words a specialist could answer or\n"
            '   argue with; it never says "thread", never numbers itself ("1/6") and carries\n'
            "   no emoji."
        )
    return (
        f"12. The first post is the hook, and it decides whether anyone reads the rest: at\n"
        f"   most {HOOK_MAX_CHARS} characters (a URL counts as {URL_CHARS}), ONE claim a "
        "specialist could\n"
        "   answer or argue with, and NO link of any kind, no thread position marker\n"
        '   ("1/6"), no "thread", no emoji. It is the sharpest line of the draft, not a\n'
        "   summary of it: the setup, the caveats and the source URL come in the later posts."
    )


# Lead-ins that exist only to introduce the link ("Source: <URL>", "Paper here:"). When the
# URL moves to the link post they are left dangling at the end of the body, so the split
# takes them with it.
_LEAD_IN = re.compile(
    r"(?:\b(?:full\s+)?(?:source|sources|paper|study|link|details|abstract|release|"
    r"read\s+it|more|results|preprint)\b[^.\n]{0,20})$",
    re.I,
)
_TRAILING_JUNK = " \t\r\n([{<\"'«—–-:;,."


def split_link_post(text: str, url: str) -> tuple[str, str] | None:
    """Turn a single or long post that carries the source URL into (body, link post).

    The pure half of run_relink.py, the one-off pass over drafts written before the link
    post existed. The URL (and the lead-in that only introduced it) comes out of the body
    and the link post is written fresh. Returns None when there is nothing to move or when
    the body would be left empty, which is the caller's signal to leave the draft alone.
    """
    if not url or url not in text:
        return None
    body = text.replace(f"({url})", " ").replace(f"[{url}]", " ").replace(url, " ")
    lines = [re.sub(r"[ \t]{2,}", " ", ln).rstrip() for ln in body.splitlines()]
    body = "\n".join(lines).strip(_TRAILING_JUNK)
    # Only a URL that ended the post can have left a lead-in dangling; one in the middle of
    # a sentence is followed by the writer's own words, which are never dropped.
    if _ended_with(text, url):
        body = _LEAD_IN.sub("", body).strip(_TRAILING_JUNK)
    if not body:
        return None
    if body[-1].isalnum() or body[-1] in ")\u201d":
        body += "."
    return body, f"Source: {url}"


def _ended_with(text: str, url: str) -> bool:
    """True when the post's last words are the URL (bare, bracketed or punctuated)."""
    tail = text.rstrip().rstrip(")]>»\"'.,;:")
    return tail.endswith(url)
