"""A story's traits, the pure part: what kind of company and story a piece or a radar topic
is about, so the learning loop can find out which kinds draw an audience without ever
learning a list of favourite companies. No DB, no network, no clock.

The account's first results came almost entirely from one company (Iovance: three posts,
the three best). Scoring stories by the companies the account has written about would keep
sending it back there and would never try a company it has not covered, so the loop learns
traits that carry over to companies it has never written about instead:

- cap: the company's size (large, mid, small, micro, private), stage: commercial, clinical
  or preclinical, beat: immuno-oncology core, adjacent or off the beat, debate: whether
  investors openly disagree about it (yes, no). The scan reports these for each topic and
  research for each piece, as it judges them from what it read.
- crowd: the retail crowd around its ticker (large, medium, small), from the number of
  people watching it on Stocktwits (studio/crowd.py), measured the same way for every
  company, covered or not.
- new_company: whether the account had written about none of its companies in the
  `ranking.novelty_days` before it (yes, no), worked out in code.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from typing import Any

from studio.radar import _norm_company

# What the scan and research report, with the values each may take.
JUDGED: dict[str, tuple[str, ...]] = {
    "cap": ("large", "mid", "small", "micro", "private"),
    "stage": ("commercial", "clinical", "preclinical"),
    "beat": ("io_core", "io_adjacent", "off_beat"),
    "debate": ("yes", "no"),
}
CROWD = ("large", "medium", "small")
YES_NO = ("yes", "no")
ARMS = ("cap", "stage", "beat", "debate", "crowd", "new_company")
VALUES: dict[str, tuple[str, ...]] = {**JUDGED, "crowd": CROWD, "new_company": YES_NO}
LABELS = {
    "cap": "Company size",
    "stage": "Company stage",
    "beat": "Beat",
    "debate": "Investors disagree",
    "crowd": "Retail crowd (Stocktwits watchers)",
    "new_company": "Company new to the account",
}

# How the scan and research are asked for the judged traits (studio/radar.py,
# studio/prompt.py), one line.
SCHEMA = (
    '{"cap": "large | mid | small | micro | private", '
    '"stage": "commercial | clinical | preclinical", '
    '"beat": "io_core | io_adjacent | off_beat", "debate": "yes | no"}'
)
GUIDE = (
    "traits describe the main company: cap is its market value (large over $10B, mid $2B "
    "to $10B, small $300M to $2B, micro under $300M, or private); stage is commercial when "
    "it sells a product, else clinical or preclinical; beat is io_core for "
    "immuno-oncology and cell therapy, io_adjacent for oncology or immunology next to it, "
    "off_beat for anything else; debate is yes when investors openly disagree about it (a "
    "loud bull and bear case, heavy short interest, a contested readout or raise)."
)

_US_TICKER = re.compile(r"^[A-Z]{1,5}$")


def read_traits(raw: Any) -> dict[str, str]:
    """The judged traits from a scan's or research's JSON, each kept only when it is one of
    the values it may take."""
    out: dict[str, str] = {}
    if not isinstance(raw, Mapping):
        return out
    for key, values in JUDGED.items():
        value = str(raw.get(key) or "").strip().lower().replace("-", "_").replace(" ", "_")
        if value in values:
            out[key] = value
    return out


def crowd_bucket(watchers: int | None, *, large: int, medium: int) -> str:
    """large, medium or small by the number of Stocktwits watchers; '' when unknown."""
    if watchers is None:
        return ""
    if watchers >= large:
        return "large"
    return "medium" if watchers >= medium else "small"


def crowd_ticker(ticker: str) -> str:
    """The ticker as Stocktwits knows a US listing (IOVA), '' for one it would not (a
    foreign listing such as 9926.HK, a blank)."""
    t = str(ticker or "").strip().upper().lstrip("$")
    return t if _US_TICKER.fullmatch(t) else ""


def company_keys(companies: Iterable[tuple[str, str]]) -> set[str]:
    """Who a list of (name, ticker) is about: each ticker, and each name normalised (Inc,
    Therapeutics and the like dropped), so "Iovance" and "Iovance Biotherapeutics (IOVA)"
    meet."""
    keys: set[str] = set()
    for name, ticker in companies:
        t = str(ticker or "").strip().upper().lstrip("$")
        if t:
            keys.add("$" + t)
        n = _norm_company(str(name or ""))
        if n:
            keys.add(n)
    return keys


def companies_of(raw: Any) -> list[tuple[str, str]]:
    """(name, ticker) pairs from a JSON list of {"name", "ticker"} (research.json, a radar
    row)."""
    out = []
    for item in raw if isinstance(raw, list) else []:
        if isinstance(item, Mapping) and str(item.get("name") or "").strip():
            out.append((str(item["name"]).strip(), str(item.get("ticker") or "").strip()))
    return out
