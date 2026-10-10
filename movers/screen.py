"""The screen, the pure part: a ticker's prices in, its moves out. No DB, no network, no
clock: `now` is a parameter.

A move is measured three ways, all against the last COMPLETED regular session (the
latest daily bar before today, or today's once the market has closed):
  regular      that session's close against the session before it;
  after_hours  the last after-hours price that evening against that close;
  premarket    the last pre-market price of the next trading day against that close.
A ticker passes when any one of them reaches the threshold, up or down, and it is liquid
enough to mean something (a close of at least `min_price`, a session's dollar volume of at
least `min_dollar_volume`, and for an extended-hours move, `min_extended_volume` shares
traded in it).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time
from typing import Any
from zoneinfo import ZoneInfo

MARKET_TZ = "America/New_York"
OPEN = time(9, 30)
CLOSE = time(16, 0)
# A daily bar for today counts as complete this long after the close, once the closing
# auction's print is in.
SETTLED = time(16, 15)

SESSIONS = ("regular", "after_hours", "premarket")
SESSION_LABELS = {
    "regular": "in the session",
    "after_hours": "after hours",
    "premarket": "pre-market",
}


class ChartError(ValueError):
    """A price answer that cannot be read."""


@dataclass(frozen=True)
class Bar:
    at: datetime  # aware, in the exchange's zone
    close: float
    volume: float


@dataclass(frozen=True)
class Chart:
    symbol: str
    tz: str
    bars: tuple[Bar, ...]
    name: str = ""


@dataclass(frozen=True)
class Move:
    session: str  # one of SESSIONS
    day: date  # the trading day it happened on
    pct: float  # signed, in percent
    start: float
    end: float
    volume: float  # shares traded in it

    def line(self) -> str:
        return (
            f"{self.pct:+.1f}% {SESSION_LABELS[self.session]} on {self.day.isoformat()} "
            f"({self.start:.2f} -> {self.end:.2f})"
        )


@dataclass
class Mover:
    ticker: str
    company: str
    session_day: date  # the last completed regular session
    close: float
    dollar_volume: float
    moves: list[Move] = field(default_factory=list)  # every move measured
    flagged: list[Move] = field(default_factory=list)  # those that pass the screen

    @property
    def biggest(self) -> Move:
        return max(self.flagged or self.moves, key=lambda m: abs(m.pct))


def parse_chart(raw: Any) -> Chart:
    """Yahoo Finance's chart answer: {"chart": {"result": [{"meta", "timestamp",
    "indicators": {"quote": [{"close", "volume"}]}}], "error"}}. Bars with no close are
    dropped. Raises ChartError when there is no usable result."""
    chart = raw.get("chart") if isinstance(raw, Mapping) else None
    if not isinstance(chart, Mapping):
        raise ChartError("not a chart answer")
    if chart.get("error"):
        err = chart["error"]
        why = err.get("description") if isinstance(err, Mapping) else err
        raise ChartError(f"the chart API said: {why}")
    results = chart.get("result")
    if not isinstance(results, list) or not results or not isinstance(results[0], Mapping):
        raise ChartError("no result")
    res = results[0]
    meta = res.get("meta") if isinstance(res.get("meta"), Mapping) else {}
    tzname = str(meta.get("exchangeTimezoneName") or MARKET_TZ)
    try:
        tz = ZoneInfo(tzname)
    except (KeyError, ValueError):
        tz, tzname = ZoneInfo(MARKET_TZ), MARKET_TZ
    stamps = res.get("timestamp") or []
    quotes = (res.get("indicators") or {}).get("quote") or [{}]
    quote = quotes[0] if isinstance(quotes[0], Mapping) else {}
    closes = quote.get("close") or []
    volumes = quote.get("volume") or []
    bars = []
    for i, stamp in enumerate(stamps if isinstance(stamps, list) else []):
        close = closes[i] if i < len(closes) else None
        if not isinstance(stamp, (int, float)) or not isinstance(close, (int, float)):
            continue
        if close <= 0:
            continue
        vol = volumes[i] if i < len(volumes) and isinstance(volumes[i], (int, float)) else 0
        at = datetime.fromtimestamp(stamp, UTC).astimezone(tz)
        bars.append(Bar(at, float(close), float(vol)))
    bars.sort(key=lambda b: b.at)
    name = str(meta.get("longName") or meta.get("shortName") or "")
    return Chart(str(meta.get("symbol") or ""), tzname, tuple(bars), name)


def _pct(start: float, end: float) -> float:
    return (end / start - 1.0) * 100.0


def measure(
    ticker: str,
    daily: Chart,
    intraday: Chart | None,
    *,
    now: datetime,
    company: str = "",
) -> Mover | None:
    """Every move of a ticker, unscreened. None without two completed sessions."""
    tz = ZoneInfo(daily.tz)
    local = now.astimezone(tz)
    today = local.date()
    settled = local.time() >= SETTLED
    done = [b for b in daily.bars if b.at.date() < today or (b.at.date() == today and settled)]
    # One bar per day (a provider may repeat the live day); the last one wins.
    by_day: dict[date, Bar] = {b.at.date(): b for b in done}
    days = sorted(by_day)
    if len(days) < 2:
        return None
    last, before = by_day[days[-1]], by_day[days[-2]]
    mover = Mover(
        ticker=ticker,
        company=company or daily.name,
        session_day=days[-1],
        close=last.close,
        dollar_volume=last.close * last.volume,
    )
    mover.moves.append(
        Move(
            "regular",
            days[-1],
            _pct(before.close, last.close),
            before.close,
            last.close,
            last.volume,
        )
    )
    if intraday is not None:
        bars = [Bar(b.at.astimezone(tz), b.close, b.volume) for b in intraday.bars]
        post = [b for b in bars if b.at.date() == days[-1] and b.at.time() >= CLOSE]
        if post:
            mover.moves.append(
                Move(
                    "after_hours",
                    days[-1],
                    _pct(last.close, post[-1].close),
                    last.close,
                    post[-1].close,
                    sum(b.volume for b in post),
                )
            )
        later = sorted({b.at.date() for b in bars if b.at.date() > days[-1]})
        if later:
            pre = [b for b in bars if b.at.date() == later[0] and b.at.time() < OPEN]
            if pre:
                mover.moves.append(
                    Move(
                        "premarket",
                        later[0],
                        _pct(last.close, pre[-1].close),
                        last.close,
                        pre[-1].close,
                        sum(b.volume for b in pre),
                    )
                )
    return mover


def screen(mover: Mover, cfg: Mapping[str, Any]) -> Mover | None:
    """The mover with its passing moves in `flagged`, or None when none pass."""
    if mover.close < float(cfg["min_price"]):
        return None
    if mover.dollar_volume < float(cfg["min_dollar_volume"]):
        return None
    threshold = float(cfg["threshold_pct"])
    mover.flagged = [
        m
        for m in mover.moves
        if abs(m.pct) >= threshold
        and (m.session == "regular" or m.volume >= float(cfg["min_extended_volume"]))
    ]
    return mover if mover.flagged else None


def benchmark_line(bench: Mover | None, symbol: str) -> str:
    if bench is None or not symbol:
        return ""
    return f"{symbol}: " + "; ".join(m.line() for m in bench.moves)


def market_time(text: Any) -> time:
    """`not_before` as a time: "08:00", or YAML's base-60 reading of an unquoted 08:00."""
    if isinstance(text, int):
        return time(min(text // 60, 23), text % 60)
    hh, _, mm = str(text or "0:0").strip().partition(":")
    return time(int(hh or 0), int(mm or 0))


def universe(root_cfg: Mapping[str, Any], cfg: Mapping[str, Any]) -> dict[str, str]:
    """ticker -> company name: the root config's companies (feeds and branding) with a
    ticker, then `extra_tickers`, less `exclude_tickers`. Order kept, no repeats."""
    out: dict[str, str] = {}
    companies = root_cfg.get("companies") or {}
    branding = root_cfg.get("branding") or {}
    entries: Sequence[Any] = [*(companies.get("feeds") or []), *(branding.get("companies") or [])]
    for entry in entries:
        if isinstance(entry, Mapping) and entry.get("ticker"):
            out.setdefault(str(entry["ticker"]).upper().strip(), str(entry.get("name") or ""))
    for ticker in cfg.get("extra_tickers") or []:
        out.setdefault(str(ticker).upper().strip(), "")
    for ticker in cfg.get("exclude_tickers") or []:
        out.pop(str(ticker).upper().strip(), None)
    return {t: n for t, n in out.items() if t}
