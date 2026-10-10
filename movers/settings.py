"""Loads movers/config.yaml (market movers' own settings; not the root config.yaml)."""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import yaml

MOVERS_DIR = Path(__file__).resolve().parent
CONFIG_PATH = MOVERS_DIR / "config.yaml"

# No model and no URL here: both live only in movers/config.yaml.
DEFAULTS: dict[str, Any] = {
    "enabled": True,
    "every_hours": 20,
    "not_before": "08:00",
    "threshold_pct": 5.0,
    "min_price": 1.0,
    "min_dollar_volume": 2_000_000,
    "min_extended_volume": 20_000,
    "benchmark": "",
    "extra_tickers": [],
    "exclude_tickers": [],
    "prices": {
        "chart_url": "",
        "daily_range": "10d",
        "intraday_range": "5d",
        "intraday_interval": "15m",
        "user_agent": "",
        "pause_seconds": 0.25,
        "timeout_seconds": 20,
    },
    "check": {
        "model": "",
        "effort": "medium",
        "timeout_minutes": 20,
        "max_checked": 12,
        "recent_days": 5,
    },
    "auto_queue": 1,
    "checkpoint": False,
}


def _merge(base: dict[str, Any], over: dict[str, Any]) -> dict[str, Any]:
    out = copy.deepcopy(base)
    for key, value in (over or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _merge(out[key], value)
        else:
            out[key] = value
    return out


def load_movers_config(path: str | Path | None = None) -> dict[str, Any]:
    with open(path or CONFIG_PATH, encoding="utf-8") as fh:
        raw = yaml.safe_load(fh) or {}
    if not isinstance(raw, dict):
        raise ValueError("movers/config.yaml must be a mapping")
    return _merge(DEFAULTS, raw)
