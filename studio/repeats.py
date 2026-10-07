"""Repeats, the pure part: does a radar topic or a catalyst cover a story the studio already
has a piece on (written, being written, or queued), perhaps under another angle? No DB, no
network, no clock.

A scan often brings back a story the account already wrote about, reworded and with a new
suggested angle, and the editor cannot always tell from the title. Each side is reduced to
its marks (source URLs, drug names and codes, trial names and NCT numbers, companies and
tickers, the title's words) and two items repeat when they share a source, a drug or a
trial, or a company and at least `MIN_SHARED_WORDS` words of their titles. The result is a
flag with its reasons, never a block: a story with genuinely new news is worth a second
piece, and the editor decides (studio/web.py's radar page; studio/prompt.py tells an
automatic piece the same).
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from draft.tags import drug_names, nct_ids
from studio.radar import _SUFFIXES

MIN_SHARED_WORDS = 2
MAX_REASONS = 3

_URL = re.compile(r"https?://[^\s<>\"')\]]+")
# A development code (AZD0486, BMS-986393, JNJ-90014496) or a trial's name (KEYNOTE-189,
# CARTITUDE-4): a capitalised word, an optional hyphen, digits.
_CODE = re.compile(r"\b([A-Z]{2,5}-?\d{3,8})\b")
_TRIAL = re.compile(r"\b([A-Z][A-Za-z]{3,})-([A-Z]?\d{1,4}[A-Za-z]?)\b")
# A cashtag. A capitalised word in brackets is not taken for a ticker: in this field it is
# as often an abbreviation (ORR, ADC, FDA).
_CASHTAG = re.compile(r"\$([A-Z]{1,6})\b")
# A meeting or a fiscal year with its year (SITC2026, ASCO-2026, FY2026) is not a drug.
_NOT_CODES = re.compile(
    r"(AACR|ASCO|ASGCT|ASH|EHA|ESMO|SABCS|SITC|SNMMI|WCLC|JPM|FY|CY)-?(19|20)\d\d"
)
# Words that take a number the way a trial's name does.
_NOT_TRIALS = frozenset({"covid", "phase", "stage", "grade", "cohort", "part", "arm", "week"})
_WORD = re.compile(r"[a-z0-9][a-z0-9\-]*")
# Words a title of this account has whatever its story: they say nothing about which story.
_COMMON = frozenset(
    """
    about after against ahead also and annual another are around based because before
    being between biotech both but can cancer cell cells clinical could data deal does
    down early first from have into investor investors just keep latest look looks make
    market means more most much news next now off once only other over phase piece
    preview reaction really says should since still story than that the their them then
    there these they this those through trial trials under update upon very want week
    what when where which while whole why will with without would year
    """.split()
)


@dataclass(frozen=True)
class Marks:
    """What identifies a story, from its words and whatever structure it came with."""

    urls: frozenset[str] = frozenset()
    ids: frozenset[str] = frozenset()  # drugs, development codes, trials, NCT numbers
    tickers: frozenset[str] = frozenset()
    companies: frozenset[str] = frozenset()  # normalised names (studio/radar.py)
    words: frozenset[str] = frozenset()  # the title's telling words
    text: str = ""  # the whole text normalised, padded with spaces, for name lookups


@dataclass(frozen=True)
class Covered:
    """A piece the studio has, or a topic queued for one, with the words it was asked for."""

    kind: str  # "piece" | "queued"
    id: int
    label: str
    angle: str = ""
    status: str = ""  # a piece's stage, or "queued"
    text: str = ""  # its topic and title
    companies: tuple[tuple[str, str], ...] = ()  # (name, ticker) from a radar row
    sources: tuple[str, ...] = ()
    drugs: tuple[str, ...] = ()  # from a catalyst it was queued from


@dataclass(frozen=True)
class Repeat:
    covered: Covered
    reasons: tuple[str, ...] = field(default_factory=tuple)

    def line(self) -> str:
        what = f"piece {self.covered.id}" if self.covered.kind == "piece" else "a queued topic"
        bits = [self.covered.status] if self.covered.status else []
        if self.covered.angle:
            bits.append(f"angle {self.covered.angle}")
        extra = f" ({', '.join(bits)})" if bits else ""
        return f"{what}{extra}: {self.covered.label}. Shares {'; '.join(self.reasons)}"


def _url_key(url: str) -> str:
    u = re.sub(r"^https?://(www\.)?", "", url.strip().lower())
    return u.split("#")[0].rstrip("/.,;")


def _norm_text(text: str) -> str:
    return " " + " ".join(re.sub(r"[^a-z0-9]+", " ", text.lower()).split()) + " "


def _company_key(name: str) -> str:
    """A company's name as `_norm_text` would write it, legal and sector suffixes dropped."""
    cleaned = _SUFFIXES.sub(" ", str(name or "").lower().replace("&", " "))
    return " ".join(re.sub(r"[^a-z0-9]+", " ", cleaned).split())


def _title_words(title: str, companies: Iterable[str]) -> frozenset[str]:
    names = {w for c in companies for w in c.split()}
    return frozenset(
        w
        for w in _WORD.findall(title.lower())
        if len(w) >= 4 and w not in _COMMON and w not in names and not w.isdigit()
    )


def marks(
    text: str,
    *,
    title: str = "",
    companies: Sequence[tuple[str, str]] = (),
    sources: Sequence[str] = (),
    drugs: Sequence[str] = (),
) -> Marks:
    """The marks of one item. `text` is everything it says (a queued topic carries its
    title, why-now, companies and sources); `title` is the line its words are taken from
    (the text's first line when blank)."""
    title = title or (text.strip().splitlines()[0] if text.strip() else "")
    urls = {_url_key(u) for u in [*sources, *_URL.findall(text)] if u}
    plain = _URL.sub(" ", text)
    ids = {d.lower() for d in drug_names(plain)}
    ids |= {d.lower() for d in drugs if d.strip()}
    ids |= {n.lower() for n in nct_ids(plain)}
    ids |= {c.lower().replace("-", "") for c in _CODE.findall(plain) if not _NOT_CODES.fullmatch(c)}
    ids |= {f"{w}-{n}".lower() for w, n in _TRIAL.findall(plain) if w.lower() not in _NOT_TRIALS}
    tickers = {t for t in (tk for _, tk in companies) if t}
    tickers |= set(_CASHTAG.findall(plain))
    names = {_company_key(n) for n, _ in companies if n}
    names = {n for n in names if len(n) >= 3}
    return Marks(
        urls=frozenset(urls),
        ids=frozenset(i for i in ids if i),
        tickers=frozenset(tickers),
        companies=frozenset(names),
        words=_title_words(title, names),
        text=_norm_text(plain),
    )


def _shared_companies(a: Marks, b: Marks) -> list[str]:
    out = sorted(a.tickers & b.tickers)
    for name in sorted(a.companies | b.companies):
        if f" {name} " in a.text and f" {name} " in b.text and name not in out:
            out.append(name)
    return out


def reasons(a: Marks, b: Marks) -> tuple[str, ...]:
    """Why `a` and `b` look like the same story, or () when they do not."""
    found: list[str] = []
    ids = sorted(a.ids & b.ids)
    if ids:
        found.append("drug or trial " + ", ".join(ids))
    if a.urls & b.urls:
        found.append("a source")
    who = _shared_companies(a, b)
    # A company's own name is not a second reason to think it is the same story.
    named = {w for c in who for w in c.lower().split()}
    words = sorted(w for w in a.words & b.words if w not in named)
    if who and len(words) >= MIN_SHARED_WORDS:
        found.append(f"company {', '.join(who)} and the words {', '.join(words)}")
    elif found and who:
        found.append(f"company {', '.join(who)}")
    return tuple(found[:MAX_REASONS])


def covered_marks(c: Covered) -> Marks:
    return marks(
        c.text,
        title=c.label,
        companies=c.companies,
        sources=c.sources,
        drugs=c.drugs,
    )


def find(item: Marks, covered: Iterable[tuple[Covered, Marks]]) -> list[Repeat]:
    """Every covered piece or queued topic `item` repeats, newest first as given."""
    out = []
    for c, m in covered:
        why = reasons(item, m)
        if why:
            out.append(Repeat(c, why))
    return out


def topic_marks(t: Any) -> Marks:
    """A radar topic's marks (studio/store.py:RadarTopic or studio/radar.py:Topic)."""
    companies = [
        (c["name"], c.get("ticker") or "") if isinstance(c, Mapping) else (c.name, c.ticker)
        for c in t.companies
    ]
    text = "\n".join(
        [t.title, t.why_now, " ".join(f"{n} ({tk})" if tk else n for n, tk in companies)]
    )
    return marks(text, title=t.title, companies=companies, sources=list(t.sources))


def catalyst_marks(c: Any) -> Marks:
    """A catalyst's marks (studio/store.py:CatalystRow or studio/radar.py:Catalyst). Its
    title is the drug and the detail, so the words matter only beside a shared company."""
    text = " ".join([c.company, c.drug, c.detail])
    return marks(
        text,
        title=f"{c.drug} {c.detail}",
        companies=[(c.company, c.ticker)],
        sources=[c.source] if c.source else [],
        drugs=[c.drug] if c.drug else [],
    )
