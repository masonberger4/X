"""Prices for the screen. `fetch_chart` is market movers' one network method: a GET of
Yahoo Finance's public chart endpoint (movers/config.yaml `prices.chart_url`, free, no key)
through ingest/http.py:get_json, which retries 429/5xx. Tests monkeypatch it."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Mapping
from typing import Any
from urllib.parse import quote

from movers import screen as SC

log = logging.getLogger(__name__)

Fetch = Callable[[str, Mapping[str, Any], Mapping[str, Any]], Any]


def fetch_chart(symbol: str, params: Mapping[str, Any], pcfg: Mapping[str, Any]) -> Any:
    from ingest import http

    return http.get_json(
        str(pcfg["chart_url"]).format(symbol=quote(symbol, safe="")),
        params=dict(params),
        user_agent=str(pcfg.get("user_agent") or "") or None,
        timeout=float(pcfg.get("timeout_seconds") or 20),
    )


def charts(
    symbol: str,
    pcfg: Mapping[str, Any],
    *,
    fetch: Fetch | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> tuple[SC.Chart, SC.Chart | None]:
    """(daily, intraday with extended hours) for one ticker. Raises for the daily chart;
    an intraday chart that fails is logged and left out (the session move still counts)."""
    get = fetch or fetch_chart
    pause = float(pcfg.get("pause_seconds") or 0)
    daily = SC.parse_chart(get(symbol, {"range": pcfg["daily_range"], "interval": "1d"}, pcfg))
    if pause:
        sleep(pause)
    try:
        intraday = SC.parse_chart(
            get(
                symbol,
                {
                    "range": pcfg["intraday_range"],
                    "interval": pcfg["intraday_interval"],
                    "includePrePost": "true",
                },
                pcfg,
            )
        )
    except Exception as exc:  # noqa: BLE001 - fail soft per chart, like a source
        log.warning("%s: no extended-hours prices (%s)", symbol, exc)
        intraday = None
    if pause:
        sleep(pause)
    return daily, intraday
