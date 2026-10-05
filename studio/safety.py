"""The few lines a studio piece may never cross, checked in code (pure).

The session writes freely; the voice guide and the playbook shape the writing, and the
cold fact-check checks the facts. Code checks only what must never reach X whatever the
writing looks like: investment or medical advice, a link, a post over its limit. These
are blocking: a piece that still has one after the polish rounds is not handed to the
queue. A target of the account's own is blocking too; an analyst's target the posts cite
is checked against piece.json by studio/qa.py:check_price_targets (draft/targets.py finds
the citations). A missing disclaimer line is a warning the editor sees on the piece.
"""

from __future__ import annotations

import re

from draft.targets import SHARE_FIGURE

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
# The account projecting a target of its own: the number a target would become, by how much
# it would move, or a value per share of its own. Saying which assumption a catalyst moves,
# which way, and so which way the target would go ("a win would push Stifel's target up",
# "the target would be unaffected") is the analysis; the new number is a price target, and
# the account sets none. Per-share figures only: a stake "worth $2.0B" is a fact.
_ABOUT = (
    r"(?:(?:about|roughly|around|nearly|approximately|at\s+least|over|north\s+of|"
    r"close\s+to|the\s+(?:low|mid|high)[-\s]?)\s*)?~?"
)
_TARGET_WORDS = (
    r"(?:(?:price\s+)?targets?|target\s+prices?|PTs?|fair\s+values?|price\s+objectives?)"
)
_MODAL = r"\b(?:would|could|should|might|will|may)\s+(?:likely\s+|probably\s+)?"
# A deal's terms are worth stating: "each share would be worth $40 in cash under the offer".
_NOT_DEAL = (
    r"(?!\s*(?:(?:a|per)\s+share\s+)?(?:in\s+cash|in\s+stock|under\s+the\s+(?:offer|deal|"
    r"merger|agreement|terms)|plus\s+(?:a|one)\s+CVR))"
)
_OWN_TARGET = (
    # "a win would lift Stifel's target to $52", "would put the target at $45", "would take
    # the price target on Iovance to roughly $30", "would lift its target by about $8"
    _MODAL + r"(?:take|put|lift|push|raise|lower|cut|move|bring|send|drive|pull)\s+"
    r"(?:\S+\s+){0,3}?" + _TARGET_WORDS + r"(?:\s+(?:on|for)\s+(?:\S+\s+){0,3}?)?\s*"
    r"(?:up\s+|down\s+)?(?:to|at|toward|towards|near|above|past|by)\s+" + _ABOUT + SHARE_FIGURE,
    # "the target would be raised to $30", "fair value should be $40", "its target will be
    # $52", "Stifel's target would move to the high $40s"
    r"\b" + _TARGET_WORDS + r"\s+(?:should|would|could|might|will|may)\s+(?:likely\s+|"
    r"probably\s+)?(?:be\s+|go\s+|rise\s+|climb\s+|fall\s+|move\s+)?(?:(?:raised|lifted|cut|"
    r"lowered|moved|taken|pushed)\s+)?(?:(?:to|at|toward|towards|near|above|past|by|"
    r"closer\s+to)\s+)?" + _ABOUT + SHARE_FIGURE,
    # "fair value closer to $30"
    r"\b(?:fair\s+value|(?:price\s+)?target|stock|shares)\s+(?:is\s+|sits\s+|looks\s+)?"
    r"closer\s+to\s+" + _ABOUT + SHARE_FIGURE,
    # "would add about $8 a share to Stifel's target", "that adds about $5 to the target"
    r"\badds?\s+"
    + _ABOUT
    + SHARE_FIGURE
    + r"\s+(?:a\s+share\s+)?to\s+(?:\S+\s+){0,3}?"
    + _TARGET_WORDS
    + r"\b",
    # the account's own: "our price target", "my base-case fair value", "our 12-month target
    # is $45", "my math puts fair value at $30"; never "not our price targets", nor a
    # quoted "our target is to file the BLA" or "our target of $450 million in revenue"
    r"(?<!\bnot\s)\b(?:my|our)\s+(?:[\w-]+\s+){0,2}?(?:price\s+targets?|target\s+prices?|"
    r"fair\s+values?|price\s+objectives?|PTs?)\b",
    r"\b(?:my|our)\s+(?:[\w-]+\s+){0,2}?targets?\s+(?:(?:is|of|at|to|stays|remains|"
    r"sits\s+at)\s+)?" + _ABOUT + SHARE_FIGURE,
    r"\b(?:my|our)\s+(?:math|numbers|model|work|estimates?)\s+(?:puts?|gets?|gives?|says?|"
    r"points?\s+to)\b[^.!?\n]{0,30}?" + SHARE_FIGURE,
    # a value per share of its own: "the stock is worth $30", "Summit is worth $30 a share",
    # "we value it at $30 a share", "I'd put the target at $45", "I see fair value closer
    # to $30", "we see $45 as fair value", "would justify $45 a share"; never a CVR's terms
    r"\b(?:stock|shares|company|it)\s+(?:is|are|would\s+be|could\s+be|should\s+be)\s+"
    r"worth\s+" + _ABOUT + SHARE_FIGURE + _NOT_DEAL,
    r"(?<!CVR\s)(?<!CVRs\s)(?<!right\s)(?<!warrant\s)\b(?:is|are|would\s+be|could\s+be|"
    r"should\s+be)\s+worth\s+" + _ABOUT + SHARE_FIGURE + r"\s*(?:a|per)\s+share\b" + _NOT_DEAL,
    r"\b(?:we|i)\s+(?:value|see|put|get|derive|arrive\s+at|calculate|model|estimate)\b"
    r"[^.!?\n]{0,40}?" + SHARE_FIGURE + r"\s*(?:a|per)\s+share\b",
    r"\b(?:we|i)(?:[’']d|\s+would)?\s+(?:value|peg)\s+(?:the\s+(?:stock|company|shares)|"
    r"it|them|\$?[A-Z][\w-]*)\s+at\s+" + _ABOUT + SHARE_FIGURE,
    r"\b(?:we|i)(?:[’']d|\s+would)?\s+(?:see|put|get|derive|arrive\s+at|calculate|"
    r"estimate)\s+(?:the\s+|a\s+|its\s+)?(?:fair\s+value|(?:price\s+)?target|value)\s+"
    r"(?:at|near|around|of|closer\s+to)\s+" + _ABOUT + SHARE_FIGURE,
    r"\b(?:we|i)(?:[’']d|\s+would)?\s+(?:see|put|get)\s+"
    + _ABOUT
    + SHARE_FIGURE
    + r"\s+(?:as\s+)?(?:fair\s+value|(?:a\s+)?(?:price\s+)?target)\b",
    _MODAL
    + r"(?:justify|support|warrant|imply|mean)\s+(?:a\s+)?"
    + _ABOUT
    + SHARE_FIGURE
    + r"\s*(?:a|per)\s+share\b",
    # by how much it would move: "would lift the target roughly 20%", "would bring the target
    # in line with Goldman's $41", "Stifel's $38 becomes $46", "upside to $45"
    _MODAL + r"(?:take|put|lift|push|raise|lower|cut|move|bring|send|drive|pull)\s+"
    r"(?:\S+\s+){0,3}?"
    + _TARGET_WORDS
    + r"\s+(?:(?:up|down)\s+)?(?:by\s+)?"
    + _ABOUT
    + r"\d+(?:\.\d+)?\s*%",
    _MODAL
    + r"(?:bring|put|take|move)\s+(?:\S+\s+){0,3}?"
    + _TARGET_WORDS
    + r"\s+in\s+line\s+with\s+(?:\S+\s+){0,2}?"
    + _ABOUT
    + SHARE_FIGURE,
    r"(?:\b" + _TARGET_WORDS + r"|['\u2019]s\s+" + SHARE_FIGURE + r")\s+(?:becomes|would\s+"
    r"become|turns\s+into)\s+" + _ABOUT + SHARE_FIGURE,
    r"\b(?:upside|downside)\s+to\s+" + _ABOUT + SHARE_FIGURE,
    # the right target, or a price the stock deserves: "a fair target is $45", "takes the
    # right target to the mid-$40s", "the stock deserves $45", "would justify a $50 target"
    r"\b(?:right|correct|true|fair|proper)\s+(?:price\s+)?targets?\b[^.!?\n]{0,30}?"
    + _ABOUT
    + SHARE_FIGURE,
    r"\b(?:deserves?|merits?)\s+(?:a\s+)?" + _ABOUT + SHARE_FIGURE,
    _MODAL + r"(?:justify|support|warrant)\s+(?:a\s+)?(?:(?:price\s+)?target\s+(?:in\s+|of\s+|"
    r"near\s+|around\s+)?"
    + _ABOUT
    + SHARE_FIGURE
    + r"|"
    + _ABOUT
    + SHARE_FIGURE
    + r"\s+(?:price\s+)?target\b)",
    r"\b" + _TARGET_WORDS + r"\s+(?:should|would|could|might|will|may)\s+(?:all|each|both)\s+"
    r"be\s+(?:(?:at|near|around|above|closer\s+to)\s+)?" + _ABOUT + SHARE_FIGURE,
    # "my sum-of-the-parts gets to $38 a share", "I get to about $46 for Stifel's target",
    # "each share would be worth $45", "Stifel should be at $50"
    r"\b(?:my|our)\s+(?:[\w-]+\s+){0,2}?(?:sum[-\s]of[-\s]the[-\s]parts|SOTP|DCF|valuation|"
    r"analysis)\s+(?:puts?|gets?(?:\s+to)?|gives?|says?|points?\s+to|lands?\s+(?:at|on))\b"
    r"[^.!?\n]{0,30}?" + SHARE_FIGURE,
    r"\b(?:we|i)(?:[\u2019']d|\s+would)?\s+(?:get\s+to|arrive\s+at|land\s+(?:at|on))\s+"
    + _ABOUT
    + SHARE_FIGURE
    + r"\s+(?:as\s+|for\s+)?(?:\S+\s+){0,2}?(?:fair\s+value|(?:price\s+)?target|a\s+share)\b",
    r"\beach\s+share\s+(?:is|would\s+be|could\s+be|should\s+be)\s+worth\s+"
    + _ABOUT
    + SHARE_FIGURE
    + _NOT_DEAL,
    r"\b(?:stock|shares|it|\$[A-Za-z]{1,5}|(?-i:[A-Z][\w-]+))\s+should\s+(?:be|trade|sit)\s+"
    r"(?:at|near|around|closer\s+to)\s+" + _ABOUT + SHARE_FIGURE,
    # a price the stock would reach: "the stock could reach $50 on a win", "Summit should
    # trade at $40 after approval"
    r"\b(?:stock|shares|it|\$[A-Za-z]{1,5}|(?-i:[A-Z][\w-]+))\s+(?:would|could|should|will|"
    r"may|might)\s+(?:likely\s+|probably\s+)?(?:reach|hit|trade\s+(?:at|to|up\s+to)|"
    r"get\s+to|rise\s+to|climb\s+to|go\s+to|re-?rate\s+to)\s+" + _ABOUT + SHARE_FIGURE,
)
_OWN_TARGET_RE = re.compile("|".join(_OWN_TARGET), re.I)
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
_DISCLAIMER_RE = re.compile(
    r"\bnot\s+(?:investment|financial)(?:\s+or\s+medical)?\s+advice\b", re.I
)
_HANDLE_RE = re.compile(r"(?<![\w@])@([A-Za-z0-9_]{1,15})\b")


def advice_problems(text: str, label: str) -> list[str]:
    """Investment advice, a price target of the account's own or medical advice in a post,
    or on a card (its text or its alt text): a card is posted too."""
    out: list[str] = []
    m = _INVESTMENT_RE.search(text)
    if m:
        words = m.group(0).lstrip(".!?\u2022:;)/\u2013\u2014- \t\n")
        out.append(f"{label} reads as investment advice ({words!r}); describe, never advise")
    out += own_target_problems(text, label)
    m = _MEDICAL_RE.search(text)
    if m:
        out.append(f"{label} reads as medical advice ({m.group(0)!r}); describe evidence only")
    return out


def blocking_problems(text: str, label: str) -> list[str]:
    """Problems in one post that keep a piece out of the queue."""
    out = advice_problems(text, label)
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


def own_target_problems(text: str, label: str) -> list[str]:
    """A price target of the account's own in a post or on a card: blocking."""
    m = _OWN_TARGET_RE.search(text)
    if not m:
        return []
    return [
        f"{label} gives a price target of the account's own ({m.group(0).strip()!r}); say "
        "which of the analyst's assumptions the catalyst moves, which way, and so which way "
        "the target would go, never the new number or by how much. A figure the firm "
        "published itself is written as that firm's case (\"Stifel's bull case, $52, "
        'assumes a win") and listed in price_targets'
    ]


def warnings(posts: list[str]) -> list[str]:
    """Softer signals for the editor, from the piece as a whole (the price targets a piece
    cites are summed up by studio/qa.py:check_price_targets)."""
    out: list[str] = []
    if posts and not _DISCLAIMER_RE.search(posts[-1]):
        out.append('the last post has no "Not investment advice." line')
    return out


def handles_in(text: str) -> list[str]:
    """Every @handle in a post, as written (without the @)."""
    return _HANDLE_RE.findall(text)
