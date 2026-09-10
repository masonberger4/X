"""Pure scheduling logic: slot math, candidate selection, breaking-news detection.

No DB and no network. Everything time-related takes an aware datetime.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import yaml

from publish.store import Approved

CONFIG_PATH = Path(__file__).with_name("config.yaml")


def load_publish_config(path: str | Path | None = None) -> dict[str, Any]:
    with open(path or CONFIG_PATH, encoding="utf-8") as fh:
        cfg = yaml.safe_load(fh) or {}
    cfg.setdefault("timezone", "UTC")
    cfg.setdefault("slots", [])
    cfg.setdefault("grace_minutes", 20)
    cfg.setdefault("max_posts_per_day", 3)
    cfg.setdefault("min_gap_minutes", 90)
    cfg.setdefault("post_format", "single")
    cfg.setdefault("breaking", {})
    cfg["breaking"].setdefault("source_prefixes", ["fda", "company_"])
    cfg["breaking"].setdefault("company_title_keywords", ["approv"])
    cfg.setdefault("policy", {})
    cfg["policy"].setdefault("prefer_breaking", True)
    cfg["policy"].setdefault("order", "score_desc")
    cfg.setdefault("media", {})
    cfg["media"].setdefault("attach_images", True)
    return cfg


def _parse_slot(slot: str) -> time:
    hh, mm = slot.split(":")
    return time(int(hh), int(mm))


def _require_aware(now: datetime) -> None:
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")


def slot_datetimes(day: date, slots: list[str], tz: str) -> list[datetime]:
    """Wall-clock slots on `day` in `tz`, as aware datetimes. DST is handled by zoneinfo."""
    zone = ZoneInfo(tz)
    out = [datetime.combine(day, _parse_slot(s), tzinfo=zone) for s in slots]
    return sorted(out)


def next_slot(now: datetime, slots: list[str], tz: str) -> datetime:
    """The first slot strictly after `now`, in `tz` local wall-clock time.

    Slots are wall-clock times, so across a DST change the gap between "now" and the slot
    is measured on the real clock (e.g. 06:00 -> 08:30 is 1.5h on spring-forward day).
    """
    _require_aware(now)
    if not slots:
        raise ValueError("no slots configured")
    local = now.astimezone(ZoneInfo(tz))
    for day_offset in (0, 1):
        for cand in slot_datetimes(local.date() + timedelta(days=day_offset), slots, tz):
            if cand > now:
                return cand
    raise AssertionError("unreachable: tomorrow always has a slot")


def open_slot(now: datetime, slots: list[str], tz: str, grace_minutes: int) -> datetime | None:
    """The slot whose window [slot, slot + grace) contains `now`, else None."""
    _require_aware(now)
    local = now.astimezone(ZoneInfo(tz))
    for cand in slot_datetimes(local.date(), slots, tz):
        if cand <= now < cand + timedelta(minutes=grace_minutes):
            return cand
    return None


def slot_label(slot: datetime) -> str:
    """Stable identifier for one slot on one day, e.g. '2026-03-08 08:30'."""
    return slot.strftime("%Y-%m-%d %H:%M")


def is_breaking(approved: Approved, breaking_cfg: dict[str, Any] | None = None) -> bool:
    """FDA items always; company PRs only when the title mentions an approval."""
    cfg = breaking_cfg or {
        "source_prefixes": ["fda", "company_"],
        "company_title_keywords": ["approv"],
    }
    source = (approved.source or "").lower()
    if not any(source.startswith(p) for p in cfg.get("source_prefixes", [])):
        return False
    if source.startswith("company_"):
        title = (approved.title or "").lower()
        return any(k.lower() in title for k in cfg.get("company_title_keywords", []))
    return True


@dataclass
class Policy:
    """Caps plus the state needed to enforce them. Built by the CLI from config + DB."""

    max_posts_per_day: int = 3
    min_gap_minutes: int = 90
    posted_today: int = 0
    last_posted_at: datetime | None = None
    prefer_breaking: bool = True
    order: str = "score_desc"
    breaking: dict[str, Any] = field(default_factory=dict)

    def blocked_reason(self, now: datetime) -> str | None:
        if self.posted_today >= self.max_posts_per_day:
            return f"daily cap reached ({self.posted_today}/{self.max_posts_per_day})"
        if self.last_posted_at is not None:
            gap = now - self.last_posted_at
            if gap < timedelta(minutes=self.min_gap_minutes):
                mins = int(gap.total_seconds() // 60)
                return f"min gap not met ({mins} < {self.min_gap_minutes} min since last post)"
        return None


def rank(approved: list[Approved], policy: Policy) -> list[Approved]:
    """Stable ordering: breaking first (if preferred), then by policy.order."""

    def key(a: Approved):
        breaking = 0 if (policy.prefer_breaking and is_breaking(a, policy.breaking)) else 1
        if policy.order == "oldest_first":
            return (breaking, a.approved_at or "", a.draft_id)
        return (breaking, -(a.score or 0.0), a.approved_at or "", a.draft_id)

    return sorted(approved, key=key)


def pick_for_slot(approved: list[Approved], slot: datetime, policy: Policy) -> Approved | None:
    """Best candidate for `slot`, or None when nothing is eligible or the caps block posting."""
    _require_aware(slot)
    if not approved or policy.blocked_reason(slot):
        return None
    return rank(approved, policy)[0]
