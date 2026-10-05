"""The few lines a studio piece may never cross, checked in code (pure).

The session writes freely; the voice guide and the playbook shape the writing, and the
cold fact-check checks the facts. Code checks only what must never reach X whatever the
writing looks like: investment or medical advice, a link, a post over its limit. These
are blocking: a piece that still has one after the polish rounds is not handed to the
queue. A few softer signals (a price target mentioned, no disclaimer line) are warnings
the editor sees on the piece.
"""

from __future__ import annotations

import re

# A directional call, a promise of returns or an instruction to trade. Describing a
# thesis, a valuation, a scenario or a published analyst view is fine.
_INVESTMENT = (
    r"\b(?:buy|sell|short|accumulate|dump|hold|load up on)\s+(?:the\s+)?(?:stock|shares|calls|puts)\b",
    r"\b(?:buy|sell|short|accumulate|dump)\s+\$[A-Za-z]{1,5}\b",
    r"\b(?:strong|clear|obvious|easy)\s+(?:buy|sell|short)\b",
    r"\b(?:you|investors|traders|everyone)\s+should\s+(?:buy|sell|short|hold|avoid|add|trim|own)\b",
    r"\b(?:my|our)\s+price\s+target\b",
    r"\bwill\s+(?:double|triple|10x|moon)\b",
    r"\bto\s+the\s+moon\b",
    r"\b(?:easy|free)\s+money\b",
    r"\bguaranteed\s+(?:return|gain|profit|win)s?\b",
    r"\bcan'?t\s+lose\b",
    r"\bload\s+up\b",
    r"\bnot\s+too\s+late\s+to\s+(?:buy|get\s+in)\b",
)
# Telling a reader what to do about their own care.
_MEDICAL = (
    r"\b(?:ask|talk\s+to)\s+your\s+(?:doctor|oncologist|physician)\b",
    r"\b(?:discuss|consult)(?:\s+(?:this|it))?\s+with\s+your\s+(?:doctor|oncologist|physician)\b",
    r"\byou\s+should\s+(?:take|try|switch|stop|start|ask\s+for)\b",
    r"\bwe\s+recommend\b",
    r"\bif\s+you\s+(?:have|are\s+being\s+treated\s+for)\s+[a-z ]{3,40}(?:,|\s)\s*(?:ask|try|consider|switch)\b",
)
_INVESTMENT_RE = re.compile("|".join(_INVESTMENT), re.I)
_MEDICAL_RE = re.compile("|".join(_MEDICAL), re.I)
_URL_RE = re.compile(r"https?://\S+", re.I)
# A bare domain is a link to X's parser too (example.com/path, www.example.com).
_BARE_DOMAIN_RE = re.compile(
    r"(?<![\w@./$-])(?:www\.[^\s]+|(?=[a-z0-9-]*[a-z])[a-z0-9-]+(?:\.[a-z0-9-]+)*\.(?:com|org|net|io|gov|edu|co|ai|bio|health|info|us|uk|eu|de|fr|ch|cn|jp|hk)(?:/[^\s]*)?)(?![\w.-])",
    re.I,
)
_PRICE_TARGET_RE = re.compile(r"\bprice\s+targets?\b", re.I)
_DISCLAIMER_RE = re.compile(r"\bnot\s+(?:investment|financial)(?:\s+or\s+medical)?\s+advice\b", re.I)
_HANDLE_RE = re.compile(r"(?<![\w@])@([A-Za-z0-9_]{1,15})\b")


def blocking_problems(text: str, label: str) -> list[str]:
    """Problems in one post that keep a piece out of the queue."""
    out: list[str] = []
    m = _INVESTMENT_RE.search(text)
    if m:
        out.append(f"{label} reads as investment advice ({m.group(0)!r}); describe, never advise")
    m = _MEDICAL_RE.search(text)
    if m:
        out.append(f"{label} reads as medical advice ({m.group(0)!r}); describe evidence only")
    m = _URL_RE.search(text) or _BARE_DOMAIN_RE.search(text)
    if m:
        out.append(f"{label} contains a link ({m.group(0)}); name the source in words")
    return out


def warnings(posts: list[str]) -> list[str]:
    """Softer signals for the editor, from the piece as a whole."""
    out: list[str] = []
    whole = "\n".join(posts)
    if _PRICE_TARGET_RE.search(whole):
        out.append(
            "mentions a price target: fine when it is a named analyst's published target, "
            "never the account's own"
        )
    if posts and not _DISCLAIMER_RE.search(posts[-1]):
        out.append('the last post has no "Not investment advice." line')
    return out


def handles_in(text: str) -> list[str]:
    """Every @handle in a post, as written (without the @)."""
    return _HANDLE_RE.findall(text)
