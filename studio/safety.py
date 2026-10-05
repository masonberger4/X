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
# thesis, a valuation, a scenario, a deal or a published analyst view is fine, so a trading
# verb is advice only as an instruction: at the start of a sentence, line or bullet, or
# after "I would", "you should", "time to" and the like. "AstraZeneca agreed to buy shares",
# "insiders now hold shares", "it would sell shares in an offering" and "$MRK to buy $VRNA"
# are the news, not a call.
_ADVICE_LEAD = (
    r"(?:(?:^|[.!?\u2022:;)/\u2013\u2014-])\s*|\b(?:you\s+should|should\s+you|you\s+must|"
    r"i\s+would|we\s+would|i[\u2019']d|i[\u2019']ll|i\s+will|i[\u2019']m|i\s+am|we[\u2019']d|"
    r"we[\u2019']re|time\s+to|definitely|gonna)\s+)"
)
_TRADE = r"(?:buy|sell|short|accumulate|dump|hold|load\s+up\s+on)"
_INVESTMENT = (
    _ADVICE_LEAD + _TRADE + r"\s+(?:the\s+)?(?:stock|shares|calls|puts)\b",
    _ADVICE_LEAD + r"(?:buy|sell|short|accumulate|dump)\s+\$[A-Za-z]{1,5}\b",
    r"\b(?:strong|clear|obvious|easy)\s+(?:buy|sell|short)(?![\w-])",
    r"\b(?:you|investors|traders|everyone)\s+should\s+(?:buy|sell|short|hold|avoid|add|trim|own)\b",
    r"\b(?:my|our)\s+price\s+target\b",
    # A price call, not a forecast for a market ("the PD-1 market will double by 2030").
    r"(?:\b(?:stock|shares|this\s+(?:one|name|stock))|\$[A-Za-z]{1,5}\b)\s+(?:will|could|can|"
    r"is\s+going\s+to|is\s+set\s+to)\s+(?:double|triple|10x|moon)\b",
    r"\bto\s+the\s+moon\b",
    r"\b(?:easy|free)\s+money\b",
    r"\bguaranteed\s+(?:return|gain|profit|win)s?\b",
    r"\bcan[\u2019']?t\s+lose\b",
    # "Time to load up", not "viral load up 20%".
    r"\btime\s+to\s+load\s+up\b|\bload(?:ing)?\s+up\s+(?:on|here|now|the\s+truck)\b",
    r"\bnot\s+too\s+late\s+to\s+(?:buy|get\s+in)\b",
)
# Telling a reader what to do about their own care.
_CARE = (
    r"(?:drug|medicine|medication|treatment|therapy|dose|dosing|trial|test|testing|biopsy|"
    r"scan|screening|combination|combo|regimen|chemo(?:therapy)?|immunotherapy|"
    r"[a-z]+(?:mab|cel|nib|lib|parib))s?\b"
)
_DISEASE = (
    r"(?:cancer|lymphoma|leuka?emia|myeloma|melanoma|carcinoma|sarcoma|glioma|glioblastoma|"
    r"tumou?rs?|nsclc|sclc|disease|[a-z]+oma)\b"
)
_MEDICAL = (
    r"\b(?:ask|talk\s+(?:to|with)|speak\s+(?:to|with)|check\s+with)\s+your\s+(?:doctor|oncologist|physician)s?\b",
    r"\b(?:discuss|consult)(?:\s+(?:this|it))?(?:\s+with)?\s+your\s+(?:doctor|oncologist|physician)s?\b",
    # "You should try the trial", not "you should take those forecasts with a grain of salt".
    r"\byou\s+should\s+(?:take|try|switch\s+to|switch|stop|start|ask\s+for|get|request)\s+"
    r"(?:[\w-]+\s+){0,3}?" + _CARE,
    r"\bwe\s+recommend\b",
    # "If you have melanoma, ask about a trial", not "if you have been following, consider".
    r"\bif\s+you\s+(?:have|are\s+being\s+treated\s+for)\s+(?:[\w-]+\s+){0,4}?"
    + _DISEASE
    + r"[^.!?\n]{0,60}?\b(?:ask|try|consider|switch|talk|enrol)",
)
_INVESTMENT_RE = re.compile("|".join(_INVESTMENT), re.I | re.M)
_MEDICAL_RE = re.compile("|".join(_MEDICAL), re.I)
_URL_RE = re.compile(r"https?://\S+", re.I)
# A bare domain is a link to X's parser too (example.com/path, www.example.com, bit.ly/x),
# also when a sentence ends on it (example.com.). The domain needs a letter, so a Hong Kong
# code like 9926.HK is not one; an exchange suffix like BAYN.DE is, and X links it.
_BARE_DOMAIN_RE = re.compile(
    r"(?<![\w@./$-])(?:www\.[^\s]+|(?=[a-z0-9-]*[a-z])[a-z0-9-]+(?:\.[a-z0-9-]+)*\.(?:com|org|net|io|gov|edu|co|ai|bio|health|info|us|uk|eu|de|fr|ch|cn|jp|hk|ly|me|tv|news|ca|au|in|it|es|nl|se|dk|fi|be|at|kr|sg|nz|br|app|dev|xyz|link|site|online|tech|page|gl|fm|science|care|cc)(?:/[^\s]*)?)(?![\w-]|\.\w)",
    re.I,
)
# A non-US listing written as ticker.exchange (GMAB.CO, NOVO-B.CO, BAYN.DE): the bare-domain
# rule catches it, since X links it, but the fix is a cashtag or words, not a source's name.
_EXCHANGE_CODE_RE = re.compile(r"[A-Z0-9][A-Z0-9-]*\.[A-Z]{1,2}")
_PRICE_TARGET_RE = re.compile(r"\bprice\s+targets?\b", re.I)
_DISCLAIMER_RE = re.compile(
    r"\bnot\s+(?:investment|financial)(?:\s+or\s+medical)?\s+advice\b", re.I
)
_HANDLE_RE = re.compile(r"(?<![\w@])@([A-Za-z0-9_]{1,15})\b")


def blocking_problems(text: str, label: str) -> list[str]:
    """Problems in one post that keep a piece out of the queue."""
    out: list[str] = []
    m = _INVESTMENT_RE.search(text)
    if m:
        words = m.group(0).lstrip(".!?\u2022:;)/\u2013\u2014- \t\n")
        out.append(f"{label} reads as investment advice ({words!r}); describe, never advise")
    m = _MEDICAL_RE.search(text)
    if m:
        out.append(f"{label} reads as medical advice ({m.group(0)!r}); describe evidence only")
    # Every link in the post at once, so one polish round can clear them all.
    found = sorted(
        [*_URL_RE.finditer(text), *_BARE_DOMAIN_RE.finditer(text)], key=lambda m: m.start()
    )
    links = list(dict.fromkeys(m.group(0) for m in found))
    codes = [x for x in links if _EXCHANGE_CODE_RE.fullmatch(x)]
    names = [x for x in links if x not in codes]
    if names:
        what = f"a link ({names[0]})" if len(names) == 1 else f"links ({', '.join(names)})"
        out.append(f"{label} contains {what}; name the source in words")
    if codes:
        out.append(
            f"{label} writes a listing as a dotted code X turns into a link ({', '.join(codes)}); "
            'give its US $cashtag, or the exchange and the ticker in words ("Copenhagen: GMAB")'
        )
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
