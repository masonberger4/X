"""Display timezone: the one place that decides what clock a human sees.

Storage never changes. Every timestamp in SQLite stays an aware-UTC ISO string
(`db.py`, and each step's `store.py`), every comparison and every window stays
UTC, and every API payload stays UTC. This module is used only at the moment a
datetime is turned into text for a person to read — the control panel, the
approval queue, the digest, and the operator reports and alerts.

The zone is config, not a constant in code: `timezone:` in the root
`config.yaml`. `publish/config.yaml` and `feedback/config.yaml` keep their own
`timezone:` keys, because those two drive behaviour (posting slots and the
"hour posted" column) rather than display; they are set to the same zone.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml

log = logging.getLogger(__name__)

#: Used when the root config.yaml is missing or has no `timezone:` key.
DEFAULT_TIMEZONE = "America/Los_Angeles"

_CONFIG_PATH = Path(__file__).resolve().parent / "config.yaml"

#: Default format for a full timestamp shown to a human, e.g. "2026-09-15 08:30 PDT".
DATETIME_FORMAT = "%Y-%m-%d %H:%M %Z"
DATE_FORMAT = "%Y-%m-%d"


@lru_cache(maxsize=1)
def timezone_name() -> str:
    """The configured display zone name, read once from the root config.yaml."""
    try:
        with open(_CONFIG_PATH, encoding="utf-8") as fh:
            cfg = yaml.safe_load(fh) or {}
        name = str(cfg.get("timezone") or DEFAULT_TIMEZONE)
    except OSError:
        return DEFAULT_TIMEZONE
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        log.warning("unknown timezone %r in config.yaml; using %s", name, DEFAULT_TIMEZONE)
        return DEFAULT_TIMEZONE
    return name


def display_tz() -> ZoneInfo:
    """The tzinfo every human-facing timestamp is rendered in."""
    return ZoneInfo(timezone_name())


def to_display(value: datetime | str | None) -> datetime | None:
    """An aware datetime in the display zone, or None.

    Accepts a datetime or a stored ISO string. A naive value is read as UTC,
    which is what every store in this repo writes.
    """
    if value is None:
        return None
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        dt = value
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(display_tz())


def fmt_datetime(value: datetime | str | None, fmt: str = DATETIME_FORMAT) -> str:
    """'2026-09-15 08:30 PDT' — empty string when there is no timestamp."""
    dt = to_display(value)
    return dt.strftime(fmt) if dt else ""


def fmt_date(value: datetime | str | None, fmt: str = DATE_FORMAT) -> str:
    """'2026-09-15', the calendar date in the display zone."""
    dt = to_display(value)
    return dt.strftime(fmt) if dt else ""


def install_jinja_filters(env) -> None:
    """Register `|localtime` and `|localdate` on a Jinja environment.

    Both accept a datetime or a stored ISO string, so a template can render a
    value straight out of SQLite without the caller parsing it first.
    """
    env.filters["localtime"] = fmt_datetime
    env.filters["localdate"] = fmt_date
