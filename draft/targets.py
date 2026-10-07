"""Where a post cites a price target (pure: no DB, no network).

An analyst's target belongs in a post only with what it rests on and whether the post's
catalyst is in it. The studio's sessions can research that (studio/qa.py:check_price_targets
holds each citation to it, and studio/safety.py blocks a target of the account's own).
Both find the citations here.

A citation is a phrase (price target, target price, price objective, the consensus, Street
or average analyst target) or a per-share figure written next to the word target, PT or
fair value. What this account's news calls a target otherwise is not one: "targets PD-1",
the median target lesion, target occupancy, a $5B target market, a revenue or EPS target, a
takeover target's deal price ("values the target at $46 a share"), a CVR's fair value per
right, a drug's target price of $150,000, results that topped Street targets.
"""

from __future__ import annotations

import re

# A per-share figure in dollars or a listing's own currency ($20, $12.40, ~$15, $1,050,
# HK$130, EUR 650, DKK 2,400), never a market size or a sales figure ($5B, $500M, $1.5 bln,
# $410-420M, $3 to $4 billion, $150,000).
_SCALE = r"\s*(?i:[bmk](?:l?n)?\b|billion|million|thousand|trillion)"
_CUR = (
    r"(?:(?:US|HK|C|A)?\$|[\u20ac\u00a3\u00a5]|"
    r"(?<![A-Za-z])(?:EUR|GBP|CHF|DKK|SEK|NOK|JPY|HKD|CAD|AUD|USD)\s?)"
)
_SHARE_FIG = (
    _CUR + r"\s?(\d,\d{3}(?:\.\d{1,2})?|\d{1,4}(?:\.\d{1,2})?)(?![\d.,]*\d)(?!" + _SCALE + r")"
    r"(?!\s*(?:[-\u2013]|to\b|or\b)\s*" + _CUR + r"?\s?\d[\d.,]*" + _SCALE + r")"
    # a price per dose or per patient ("$35 per dose") is a cost, not a share's value
    r"(?!\s*(?:(?:a|an|per)\s+|/\s*)(?i:dose|patient|vial|infusion|treatment|course|cycle|"
    r"unit|month|year|pill|tablet)s?\b)"
)
# The same per-share figure for other checks (one group: the number): studio/safety.py's
# check on a target of the account's own.
SHARE_FIGURE = _SHARE_FIG
# Words that may stand between a figure and the word target: "$12.40 consensus target".
_TARGET_ADJ = (
    r"(?:(?:consensus|average|mean|median|street(?:-high|-low)?|new|old|previous|prior|"
    r"raised|lowered|reduced|analyst|analysts['\u2019]|12-month|one-year|high|low|highest|"
    r"lowest|(?:bull|bear|base)(?:[-\s]case)?|blended)\s+){0,3}"
)
# The word a figure is cited as: a target, a target price, BofA's price objective or a fair
# value (estimate).
_TARGET_WORD = (
    r"(?:(?:price\s+)?targets?(?:\s+prices?)?|price\s+objectives?|"
    r"fair[-\s]+values?(?:\s+estimates?)?)"
)
# What a target is in a readout or a lab, not a price: "the consensus target sequence".
_NOT_READOUT = (
    r"(?![-\s]+(?:lesions?|occupancy|engagement|coverage|saturation|doses?|dosing|"
    r"exposures?|levels?|populations?|enrol\w*|antigens?|cells?|expression|binding|sizes?|"
    r"trough|concentrations?|tissues?|genes?|sites?|sequences?|validation|product|"
    r"profiles?|organs?|proteins?|volumes?|receptors?|mutations?|tumou?rs?|"
    r"densit(?:y|ies)|ratios?|burden|sums?|AUC|accrual|attainment|lysis|killing|affinity|"
    r"epitopes?)\b)(?!-to-)"
)
_ANALYST = (
    r"(?:analyst|analysts['\u2019]?|analyst['\u2019]s|consensus|(?:wall\s+)?street"
    r"(?:-high|-low|['\u2019]s)?|sell-side)"
)
_TARGET_PHRASE_RE = re.compile(
    r"\bprice[-\s]+(?:targets?|objectives?)\b"
    # a drug's target price runs to tens of thousands: "a target price of $150,000"
    r"|\btarget\s+prices?\b(?![^.;\n]{0,40}?(?:\d{2,3},\d{3}|\d[\d.,]*\s*(?:k|thousand)\b))"
    # the average or consensus of the analysts' targets, never "the highest target dose"
    r"|\b(?:(?:average|mean|median|highest|lowest|high|low)\s+)?"
    + _ANALYST
    + r"\s+(?:(?:average|mean|median)\s+)?(?:price\s+)?targets?\b"
    + _NOT_READOUT
    # "the consensus target in gastric cancer" is biology; "for Iovance is $12.40" is not
    + r"(?!\s+(?:in|for|among|across|of)\b(?![^.;\n]{0,30}?\$\s?\d))"
    # a sales consensus: "topped the consensus target of $7.9B"
    + r"(?!\s+(?:of|at|is|was)\s+~?"
    + _CUR
    + r"?\s?[\d.,]+"
    + _SCALE
    + r")"
    # "Goldman's target implies 70% upside"
    r"|\btargets?\s+(?:implies|implied|imply|suggests?|points?\s+to)\s+(?:(?:about|roughly|"
    r"nearly|over|more\s+than)\s+)?~?\d[\d.,]*%\s+(?:upside|downside)\b",
    re.I,
)
# "a $15 target", "$12.40 consensus target", "$38-per-share target", "$121 fair-value
# estimate", "a $20-$25 target range"; never "$46 target shareholders" (a deal's).
_FIG_BEFORE_RE = re.compile(
    r"(?:"
    + _SHARE_FIG
    + r"\s*[-\u2013]\s*)?"
    + _SHARE_FIG
    + r"(?:[-\s]+(?:per|a)[-\s]+share)?\s+"
    + _TARGET_ADJ
    + _TARGET_WORD
    + r"\b"
    r"(?![-\s]+(?:share|stock)?holders?\b|\s+compan(?:y|ies)\b|['\u2019]s\b|\s+costs?\b)",
    re.I,
)
# What a target is on: "on Iovance", "on shares of Allogene", "for BioNTech", "on the stock".
_ON_WHAT = (
    r"(?:\s+(?:on|for)\s+(?:the\s+(?:stock|shares|company|name)|it|"
    r"(?:shares\s+of\s+)?(?-i:\$?[A-Z][\w.&'\u2019-]*(?:\s+[A-Z][\w.&'\u2019-]*){0,2})))?"
)
# "target to $20", "target of $38", "price target (~$12.40", "fair value near $30", "price
# target on Iovance to $20", "target was raised to $20", "target is now $20", "targets range
# from $8 to $30", "target at a Street-high $50", "Price target: $52", and the appositive
# "Jefferies' target, $40, assumes". Never a fair value per right or warrant, or a deal's
# price in cash.
_FIG_AFTER_RE = re.compile(
    r"\b"
    + _TARGET_WORD
    + _ON_WHAT
    + r"(?:\s*,?\s+(?:is|was|has\s+been|had\s+been|now|still|(?:would|could|should|might|"
    r"will|may)(?:\s+(?:be|go|rise|fall|move))?))*"
    r"(?:\s+(?:raised|lifted|increased|boosted|bumped|hiked|cut|lowered|reduced|trimmed|"
    r"slashed|moved|went|goes|rose|fell|range[sd]?|ranging|runs?|sits?|stands?|remains|"
    r"stays|averages?|averaged))?"
    r"\s*:?\s+(?:(?:to|of|at|from|is|was|near|around|above|below|between)\s+)?"
    r"(?:(?:about|roughly|around|nearly|approximately|just|only|a\s+street-(?:high|low))\s+)?"
    r"[(~]?\s*~?" + _SHARE_FIG + r"(?!\s*per\s+(?:right|cvr|warrant|option|unit)\b)"
    # a company's earnings goal: "targets $10.50 in 2028 EPS"
    r"(?!\s+(?:in\s+)?(?:(?:FY|fiscal\s+)?\d{4}\s+|adjusted\s+|non-GAAP\s+)*"
    r"(?:EPS|earnings|sales|revenue|profit)\b)"
    r"(?!\s*(?:(?:a|per)\s+(?:share|ads)\s+)?in\s+cash\b)"
    r"|\b" + _TARGET_WORD + r",\s+(?:now\s+)?~?" + _SHARE_FIG + r"(?=\s*,)",
    re.I,
)
# What follows a cited figure with the move: "to $20 from $9", "$38 (cut from $45)", and
# a second firm whose target goes unsaid: ", and Wells Fargo to $18". Never "vs $14 today":
# a target set against the share price is one target.
_FIG_CHAIN_RE = re.compile(
    r"\s*[(,]?\s*(?:(?:cut|raised|lowered|reduced|trimmed|up|down|moved)\s+)?"
    r"(?:from|to)\s+~?"
    + _SHARE_FIG
    # "PT $38 (vs. $45)", "$38 (vs $45 prior)", and the arrow of notes: "$4\u2192$7"
    + r"|\s*\(\s*(?:vs\.?|versus)\s+~?"
    + _SHARE_FIG
    + r"(?=\s*(?:prior|previous(?:ly)?|before|earlier|old)?\s*\))"
    + r"|\s*,?\s*(?:vs\.?|versus)\s+~?"
    + _SHARE_FIG
    + r"(?=\s+(?:prior|previous(?:ly)?|before|earlier|old)\b)"
    + r"|\s*(?:\u2192|->)\s*~?"
    + _SHARE_FIG
    + r"|,?\s*(?:and|while)\s+(?:[A-Z][\w.&'\u2019-]*\s+){1,3}(?:(?:raised|cut|lowered|"
    r"moved|went)\s+)?(?:to|at)\s+~?" + _SHARE_FIG
)
# After targets in the plural, a bare second figure is one too: "raised targets to $20 and
# $18", "targets of $20 (H.C. Wainwright) and $18 (Wells Fargo)".
_AND_FIG_RE = re.compile(r"\s*(?:\([^)]{1,40}\))?\s*,?\s*and\s+~?" + _SHARE_FIG)
_PLURAL_RE = re.compile(r"\btargets\b|\bPTs\b", re.I)
# "PT $25", "PT to $25", "PT raised to $40", "PT: $38", "$25 PT" and the "TP HK$130" of
# Asian research (the abbreviations only in capitals, only with a figure). Never "PO": that is
# a public offering ("priced a $200M PO at $18"); BofA's price objective is written out.
_PT_RE = re.compile(
    r"(?<![A-Za-z])(?:PT|TP)s?:?(?:\s+(?:raised|lifted|increased|cut|lowered|reduced|"
    r"trimmed|moved|goes|went|now))?\s+(?:(?:to|of|at|from|is)\s+)?~?"
    + _SHARE_FIG
    + r"|"
    + _SHARE_FIG
    + r"\s+(?:PT|TP)s?(?![A-Za-z])"
)
# The shorthand of a fact base or a ratings table: "Stifel Buy $38 (from $45)", "H.C.
# Wainwright Neutral at $23", "Goldman Buy, $41" (the rating capitalised, with a figure), and
# a consensus given as a figure, "consensus ~$28.64" (not an EPS consensus).
_RATING_RE = re.compile(
    r"(?<![\w$])(?:Strong\s+Buy|Buy|Overweight|Outperform|Neutral|Hold|Sell|Underweight|"
    r"Underperform|(?:Market|Sector|Peer)\s+Perform|Equal[-\s]?Weight|In-Line)"
    r"(?:[-\s]rated)?,?\s+(?:(?:at|with)\s+(?:an?\s+)?)?~?"
    + _SHARE_FIG
    + r"(?!\s*(?:a|per)\s+share\b)"
)
_CONSENSUS_FIG_RE = re.compile(
    r"\bconsensus\s+(?:(?:sits|stands|is|was|now)\s+)?(?:(?:of|at|near|around|about)\s+)?"
    r"~?" + _SHARE_FIG + r"(?!\s*(?:EPS|loss|earnings|in\s+(?:EPS|earnings)|(?:a|per)\s+share\b))",
    re.I,
)
# "Stifel and Guggenheim both cut to $38", "H.C. Wainwright cut to $23 from $30": a firm's
# move with no target word (the subject capitalised, never the stock or a price).
_MOVE_RE = re.compile(
    r"(?<![\w$])(?!(?:Shares|Stock|The|It|Its|This|That|Price|Prices)\b)[A-Z][\w.&'\u2019-]*\s+"
    r"(?:(?:both|each|also|then|just|has|have|had)\s+){0,2}"
    r"(?:cut|raised|lowered|trimmed|reduced|hiked)\s+(?:(?:it|its|their)\s+)?"
    r"(?:to|from)\s+~?" + _SHARE_FIG + r"(?!\s*(?:a|per)\s+share\b)"
)
# A firm's published case given as a figure: "Stifel's bull case, $52", "its bear case
# ($20)", "a bull-case value of $60".
_CASE_FIG_RE = re.compile(
    r"\b(?:bull|bear|base)[-\s]case(?:\s+(?:target|value|price|scenario))?\s*[,:(]?\s*"
    r"(?:(?:of|at|is|was|sits\s+at)\s+)?~?" + _SHARE_FIG,
    re.I,
)
# The words just before a match that make "target" something else: a takeover target and its
# deal price ("values the target at $46 a share"), a company's EPS, sales or cost target, a
# CVR's or a warrant's fair value, and results that topped the Street's targets.
_NOT_A_PRICE_RE = re.compile(
    r"(?:\b(?:takeover|acquisition|buyout|M&A|deal)\s+"
    r"|\b(?:values?|valued|valuing|offers?|offered|offering|pays?|paid|paying|buys?|"
    r"buying|bought|acquires?|acquired|acquiring|(?:offer|bid|price|deal|tender)\s+for|"
    r"(?:acquisition|purchase|shares|holders|stockholders|shareholders)\s+of)\s+the\s+"
    r"|\b(?:EPS|earnings|dividend|sales|revenue|cost|COGS|savings|margin|enrol?lment|"
    r"CVRs?|warrants?|options?|rights?)(?:['\u2019]s?)?\s+(?:(?:per\s+(?:share|dose|"
    r"patient|unit|infusion)|price)\s+)?(?:(?:has|had|have|with|hit|met|of)\s+"
    r"(?:a|an|its|their|the)\s+)?"
    r"|\b(?:top(?:s|ped|ping)?|beat(?:s|ing)?|miss(?:es|ed|ing)?|exceed(?:s|ed|ing)?|"
    r"surpass(?:es|ed|ing)?)\s+(?:the\s+|its\s+|their\s+)?(?:\w+\s+)?)$",
    re.I,
)
# A link's path is not a citation: ".../summit-therapeutics-price-target-harmoni-data/".
_LINK_RE = re.compile(r"https?://\S+|\b(?:www\.)?[a-z0-9-]+(?:\.[a-z0-9-]+)*\.[a-z]{2,}/\S*", re.I)
# An earnings or sales figure set against its consensus, in the same sentence or the one
# before: "EPS of $2.58 vs consensus $2.35", "Q2 loss per share was $0.28. Consensus was $0.31".
_RESULTS_BEFORE_RE = re.compile(
    r"\b(?:EPS|earn(?:ed|s|ings)|revenue|sales|guidance|profit|loss(?:es)?|"
    r"(?:a|per)\s+share)\b",
    re.I,
)
_FIGURE_RES = (
    _FIG_BEFORE_RE,
    _FIG_AFTER_RE,
    _PT_RE,
    _RATING_RE,
    _CONSENSUS_FIG_RE,
    _MOVE_RE,
    _CASE_FIG_RE,
)
_CITING = (_TARGET_PHRASE_RE, *_FIGURE_RES)


def _plain(text: str) -> str:
    """The text with every link blanked out, so offsets stay the same."""
    return _LINK_RE.sub(lambda m: " " * len(m.group(0)), text)


def _citations(text: str, regexes: tuple[re.Pattern[str], ...]) -> list[re.Match[str]]:
    """Every match of `regexes` in `text` that is a price target, not a deal's or a
    company's target."""
    out = []
    for rx in regexes:
        for m in rx.finditer(text):
            if _NOT_A_PRICE_RE.search(text, max(0, m.start() - 40), m.start()):
                continue
            if rx is _CONSENSUS_FIG_RE and _RESULTS_BEFORE_RE.search(
                text, max(0, m.start() - 80), m.start()
            ):
                continue
            out.append(m)
    return out


def _figures_of(m: re.Match[str]) -> list[tuple[int, str]]:
    return [
        (m.start(k), m.group(k).replace(",", ""))
        for k in range(1, (m.lastindex or 0) + 1)
        if m.group(k) is not None
    ]


def target_figures(text: str) -> list[str]:
    """The per-share figures a text gives as price targets, in order, without the currency
    or a thousands comma: "raised its target to $20 from $9" gives ["20", "9"]. Only figures
    written next to the word target (or PT) count; a share price or a market size elsewhere
    in the sentence does not."""
    text = _plain(text)
    found: list[tuple[int, str]] = []
    for m in _citations(text, _FIGURE_RES):
        found += _figures_of(m)
        chains = [_FIG_CHAIN_RE] + ([_AND_FIG_RE] if _PLURAL_RE.search(m.group(0)) else [])
        end = m.end()  # "$38 target (cut from $45)", ", and Guggenheim at $40"
        while chained := next((c for rx in chains if (c := rx.match(text, end))), None):
            found += _figures_of(chained)
            end = chained.end()
    return list(dict.fromkeys(fig for _, fig in sorted(found)))


def target_mentions(text: str) -> list[str]:
    """Where a text cites a price target, each as the words that show it ("price target of
    $40", "target to $20", "$15 target"); [] when it cites none. "Targets PD-1", "a $5B
    target market" and "the target population" are not price targets."""
    text = _plain(text)
    spans = sorted((m.start(), m.end()) for m in _citations(text, _CITING))
    merged: list[list[int]] = []
    for start, end in spans:  # overlapping matches are one citation
        if merged and start < merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return list(dict.fromkeys(" ".join(text[s:e].split()) for s, e in merged))


_SHARE_FIG_RE = re.compile(_SHARE_FIG, re.I)


def share_figures(text: str) -> list[str]:
    """Every per-share figure in a text, as target_figures gives them ("$38 (from $45)" gives
    ["38", "45"]; "$2.0B" none): for a cell of a table of targets."""
    return [m.group(1).replace(",", "") for m in _SHARE_FIG_RE.finditer(_plain(text))]


def field_figures(text: str) -> list[str]:
    """Every figure a field gives ("bull $60, bear $20" gives ["60", "20"]): those with a
    currency, else every number."""
    found = [f.replace(",", "") for f in _FIELD_FIG_RE.findall(text)]
    return found or [f.replace(",", "") for f in _BARE_FIG_RE.findall(text)]


_FIELD_FIG_RE = re.compile(_CUR + r"\s?(\d{1,3}(?:,\d{3})+(?:\.\d{1,2})?|\d+(?:\.\d{1,2})?)")
_BARE_FIG_RE = re.compile(r"\d{1,3}(?:,\d{3})+(?:\.\d{1,2})?|\d+(?:\.\d{1,2})?")


def field_figure(text: str) -> str | None:
    """The figure a field such as piece.json's target or previous gives ("$20 (note of
    2026-09-30)" gives "20", "HK$130" "130", "$1,200" "1200"): its first figure with a
    currency, else its first number; None when it has none."""
    m = _FIELD_FIG_RE.search(text)
    if m:
        return m.group(1).replace(",", "")
    bare = _BARE_FIG_RE.search(text)
    return bare.group(0).replace(",", "") if bare else None


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
