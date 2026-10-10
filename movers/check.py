"""The story check: is there a story behind each mover, and is it worth a piece.

`check_prompt` and `parse_check` are pure. `call_checker` is the check's one model call:
claude_cli.run_claude with WebSearch and WebFetch only, on movers/config.yaml's `check`
model and effort, with its own time limit. One call covers every new mover of a screen.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from movers import screen as SC

CHECK_TOOLS = ("WebSearch", "WebFetch")
CAUSES = (
    "data",  # trial results, a conference presentation, a publication
    "regulatory",  # FDA/EMA decision, CRL, clinical hold, label
    "deal",  # M&A, licensing, partnership
    "financing",  # offering, PIPE, ATM, debt
    "earnings",  # results, guidance, sales
    "analyst",  # an upgrade, downgrade or initiation
    "legal",  # litigation, patents
    "management",  # executive change, restructuring, layoffs
    "sector",  # the whole group moved, macro, policy, sympathy with a peer
    "other",
    "unknown",  # no news found
)
MAX_TITLE = 200
MAX_TEXT = 800
MAX_SOURCES = 5
_URL = re.compile(r"^https?://[^\s<>\"']{3,500}$")

SYSTEM = """You are the research desk of an X account on the business and investing side of
immuno-oncology biotech (CAR-T and cell therapy, T-cell engagers and bispecifics, adjacent
IO science, and the wider biotech tape its readers trade). Plain code screened the market
and found the stocks below that moved {threshold:g}% or more. For EACH one, find out why it
moved: search the news (the company's press releases and SEC filings first, then trade
press and the wires) for the day of the move and the evening before. Be quick and factual;
you are not writing the post, only saying whether there is a story and what it is.

A story is worth a piece when the cause is news an immuno-oncology/biotech analyst can
interpret for investors: trial data, a regulatory decision, a deal, a financing that says
something, guidance. It is NOT worth a piece when no cause can be found, when the stock just
moved with the sector or a peer, on an index change, a thin-volume drift, or when it is the
same story as one already covered (listed under each ticker). Never invent a cause.

Answer with ONE JSON object and nothing else:
{{
  "summary": "<one line on the day's movers>",
  "movers": [
    {{
      "ticker": "<as given>",
      "story": <true if you found why it moved, else false>,
      "cause": "<one of: {causes}>",
      "what_happened": "<two or three sentences: the news, with its numbers, and the date>",
      "worth_a_piece": <true or false, as above>,
      "title": "<if worth a piece: the piece's working title, specific>",
      "why_now": "<if worth a piece: why this is the account's story today>",
      "angle": "<if worth a piece: the angle key that fits best from the list, or empty>",
      "sources": ["<http(s) URLs you read that show the cause, best first>"],
      "rank": <1 for the mover most worth a piece, 2 for the next; 0 when not worth one>
    }}
  ]
}}
Include every ticker you were given, once."""


def check_prompt(
    movers: Sequence[SC.Mover],
    *,
    today: str,
    threshold: float,
    benchmark: str,
    angles: Mapping[str, str],
    earlier: Mapping[str, Sequence[str]],
) -> tuple[str, str]:
    """(system, user) for one check over `movers`. `earlier` lists, per ticker, the mover
    stories already found for it lately."""
    system = SYSTEM.format(threshold=threshold, causes=", ".join(CAUSES))
    lines = [f"Today is {today} (US market dates are New York dates).", ""]
    if benchmark:
        lines += [f"The sector benchmark for context: {benchmark}", ""]
    lines.append("MOVERS")
    for m in movers:
        who = f"{m.ticker} ({m.company})" if m.company else m.ticker
        lines.append(f"- {who}: " + "; ".join(x.line() for x in m.flagged))
        for story in earlier.get(m.ticker, ()):
            lines.append(f"  Already covered: {story}")
    lines += ["", "ANGLES (key: the question a piece at that angle answers)"]
    lines += [f"- {key}: {question}" for key, question in angles.items()]
    return system, "\n".join(lines)


class CheckRejected(ValueError):
    """The check's answer could not be used; nothing is stored from it."""


@dataclass(frozen=True)
class Verdict:
    ticker: str
    story: bool
    cause: str
    what_happened: str
    worth: bool
    title: str = ""
    why_now: str = ""
    angle: str = ""
    sources: tuple[str, ...] = ()
    rank: int = 0


@dataclass
class CheckResult:
    summary: str = ""
    verdicts: list[Verdict] = field(default_factory=list)
    dropped: list[str] = field(default_factory=list)


def _text(value: Any, limit: int) -> str:
    one = " ".join(str(value or "").split()) if isinstance(value, (str, int, float)) else ""
    return one[:limit].rstrip()


def _urls(value: Any) -> tuple[str, ...]:
    out: list[str] = []
    for item in value if isinstance(value, list) else []:
        url = str(item or "").strip()
        if _URL.fullmatch(url) and url not in out:
            out.append(url)
    return tuple(out[:MAX_SOURCES])


def parse_check(text: str, *, tickers: Iterable[str], angles: Iterable[str]) -> CheckResult:
    """The check's JSON answer, checked: only tickers that were asked about, each once; a
    piece needs a title; an unknown angle or cause is blanked. Raises CheckRejected when
    it is not a JSON object."""
    raw = text.strip()
    start, end = raw.find("{"), raw.rfind("}")
    if start < 0 or end <= start:
        raise CheckRejected("the answer is not a JSON object")
    try:
        data = json.loads(raw[start : end + 1])
    except ValueError as exc:
        raise CheckRejected(f"the answer is not valid JSON ({exc})") from exc
    if not isinstance(data, Mapping):
        raise CheckRejected("the answer is not a JSON object")
    asked, known = {t.upper() for t in tickers}, set(angles)
    out = CheckResult(summary=_text(data.get("summary"), MAX_TEXT))
    seen: set[str] = set()
    for item in data.get("movers") if isinstance(data.get("movers"), list) else []:
        if not isinstance(item, Mapping):
            out.dropped.append("a mover that is not an object")
            continue
        ticker = _text(item.get("ticker"), 16).upper().lstrip("$")
        if ticker not in asked or ticker in seen:
            out.dropped.append(f"{ticker or 'a mover'}: not asked about, or repeated")
            continue
        seen.add(ticker)
        story = item.get("story") is True
        title = _text(item.get("title"), MAX_TITLE)
        worth = story and item.get("worth_a_piece") is True and bool(title)
        cause = _text(item.get("cause"), 20).lower()
        angle = _text(item.get("angle"), 40)
        rank = item.get("rank")
        out.verdicts.append(
            Verdict(
                ticker=ticker,
                story=story,
                cause=cause if cause in CAUSES else ("other" if story else "unknown"),
                what_happened=_text(item.get("what_happened"), MAX_TEXT),
                worth=worth,
                title=title if worth else "",
                why_now=_text(item.get("why_now"), MAX_TEXT) if worth else "",
                angle=angle if worth and angle in known else "",
                sources=_urls(item.get("sources")),
                rank=rank if worth and isinstance(rank, int) and not isinstance(rank, bool) else 0,
            )
        )
    missing = sorted(asked - seen)
    if missing:
        out.dropped.append("no answer for " + ", ".join(missing))
    return out


def best_first(verdicts: Iterable[Verdict]) -> list[Verdict]:
    """The verdicts worth a piece, by the check's rank (unranked last), then as given."""
    worth = [v for v in verdicts if v.worth]
    return sorted(worth, key=lambda v: v.rank if v.rank > 0 else 10_000)


def topic_text(v: Verdict, mover: SC.Mover, *, screened: str) -> str:
    """The words a mover becomes as a queued studio topic. The angle is only a suggestion:
    the studio's session picks the angle and writes in the voice the app gives it."""
    who = f"{mover.company} ({mover.ticker})" if mover.company else mover.ticker
    lines = [
        v.title,
        f"Market mover ({screened}): {who} " + "; ".join(m.line() for m in mover.flagged),
    ]
    if v.what_happened:
        lines.append(f"Why it moved: {v.what_happened}")
    if v.why_now:
        lines.append(f"Why now: {v.why_now}")
    if v.angle:
        lines.append(f"An angle that may fit (yours to choose): {v.angle}")
    if v.sources:
        lines.append("Sources to start from: " + " ".join(v.sources))
    return "\n".join(lines)


def call_checker(
    system: str,
    user: str,
    *,
    model: str,
    effort: str,
    root_cfg: dict[str, Any],
    timeout: float,
) -> str:
    """The check's one CLI call: web search and fetch only, its own time limit."""
    import claude_cli

    cli = {**(root_cfg.get("claude_code") or {}), "timeout_seconds": timeout}
    return claude_cli.run_claude(
        user,
        system=system,
        model=model,
        cfg={**root_cfg, "claude_code": cli},
        effort=effort or None,
        tools=list(CHECK_TOOLS),
    )
