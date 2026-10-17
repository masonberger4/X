"""The retail crowd around a ticker: how many people watch it on Stocktwits, read from its
free public symbol page (no key, no account), so the radar can tell a company with a crowd
like Iovance's from one nobody follows, whether or not the account has written about it.

`fetch_watchers` is this module's one network call and goes through ingest/http.py
(retries, the user agent); `refresh` looks up the tickers whose last count is older than
`ranking.crowd_refresh_hours`, at most `ranking.crowd_max_lookups` a run, and stores each in
studio_crowd. It never raises: Stocktwits changing or refusing its page leaves the last
counts (and a note of why) in place, and the radar carries on without the crowd.
"""

from __future__ import annotations

import logging
import sqlite3
import time
from collections.abc import Callable, Iterable, Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

from studio import store as S
from studio import traits as TR

log = logging.getLogger(__name__)


class CrowdError(RuntimeError):
    """A lookup that gave no count."""


def fetch_watchers(ticker: str, rcfg: Mapping[str, Any]) -> int:
    """People watching `ticker` on Stocktwits (symbol.watchlist_count on its page)."""
    from ingest import http

    url = str(rcfg["crowd_url"]).format(ticker=ticker)
    try:
        data = http.get_json(url, user_agent=str(rcfg.get("crowd_user_agent") or "") or None)
    except Exception as exc:  # any HTTP or parse failure is one ticker's lookup failing
        raise CrowdError(f"{type(exc).__name__}: {exc}"[:200]) from exc
    symbol = data.get("symbol") if isinstance(data, Mapping) else None
    count = symbol.get("watchlist_count") if isinstance(symbol, Mapping) else None
    if isinstance(count, bool) or not isinstance(count, int) or count < 0:
        raise CrowdError("no watchlist_count on the page")
    return count


def due(
    tickers: Iterable[str], known: Mapping[str, S.CrowdRow], *, now: datetime, hours: float
) -> list[str]:
    """The tickers to look up, in the order given: never looked up, or looked up more than
    `hours` ago. Each US ticker once; a foreign listing is skipped."""
    out: list[str] = []
    for raw in tickers:
        t = TR.crowd_ticker(raw)
        if not t or t in out:
            continue
        row = known.get(t)
        if row is not None:
            try:
                when = datetime.fromisoformat(row.fetched_at)
            except ValueError:
                when = None
            if when is not None and now - when < timedelta(hours=hours):
                continue
        out.append(t)
    return out


def refresh(
    conn: sqlite3.Connection,
    rcfg: Mapping[str, Any],
    tickers: Iterable[str],
    *,
    fetch: Callable[[str, Mapping[str, Any]], int] | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> tuple[int, int]:
    """Look up the tickers that are due; (counted, failed). Never raises."""
    if not rcfg.get("crowd_enabled", True) or not rcfg.get("crowd_url"):
        return 0, 0
    try:
        todo = due(
            tickers,
            S.crowd(conn),
            now=datetime.now(UTC),
            hours=float(rcfg.get("crowd_refresh_hours") or 24),
        )[: max(0, int(rcfg.get("crowd_max_lookups") or 0))]
    except sqlite3.Error:
        log.warning("could not read the crowd counts", exc_info=True)
        return 0, 0
    counted = failed = 0
    pause = float(rcfg.get("crowd_delay_seconds") or 0)
    for i, ticker in enumerate(todo):
        if i and pause > 0:
            sleep(pause)
        try:
            S.set_crowd(conn, ticker, (fetch or fetch_watchers)(ticker, rcfg))
            counted += 1
        except CrowdError as exc:
            failed += 1
            log.info("crowd: %s: %s", ticker, exc)
            try:
                S.set_crowd(conn, ticker, None, error=str(exc))
            except sqlite3.Error:
                pass
        except sqlite3.Error:
            log.warning("could not store the crowd count of %s", ticker, exc_info=True)
            failed += 1
    if todo:
        log.info("crowd: %s ticker(s) counted, %s failed", counted, failed)
    return counted, failed
