"""Pure scheduling logic: slot math, candidate selection, breaking-news detection.

No DB and no network. Everything time-related takes an aware datetime.
"""

from __future__ import annotations

import re
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
    cfg.setdefault("retry", {})
    cfg["retry"].setdefault("auto_release_failed", True)
    cfg["retry"].setdefault("max_attempts", 3)
    cfg.setdefault("media", {})
    cfg["media"].setdefault("attach_images", True)
    return cfg


CAPS = ("max_posts_per_day", "min_gap_minutes")


def save_caps(
    max_posts_per_day: int, min_gap_minutes: int, path: str | Path | None = None
) -> dict[str, int]:
    """Write the two hard caps back into publish/config.yaml, editing just their lines so
    the file's comments survive (the control panel's publishing page calls this). A key
    missing from the file is appended. Values are validated here: at least one post a
    day, a gap of zero or more minutes."""
    values = {"max_posts_per_day": int(max_posts_per_day), "min_gap_minutes": int(min_gap_minutes)}
    if values["max_posts_per_day"] < 1:
        raise ValueError("max_posts_per_day must be at least 1")
    if values["min_gap_minutes"] < 0:
        raise ValueError("min_gap_minutes cannot be negative")
    target = Path(path or CONFIG_PATH)
    lines = target.read_text(encoding="utf-8").splitlines(keepends=True)
    seen: set[str] = set()
    for i, line in enumerate(lines):
        for key in CAPS:
            if re.match(rf"^{key}\s*:", line):
                comment = line.split("#", 1)[1] if "#" in line else ""
                tail = f"  #{comment}" if comment else "\n"
                lines[i] = f"{key}: {values[key]}{tail}"
                seen.add(key)
    if lines and not lines[-1].endswith("\n"):
        lines[-1] += "\n"
    for key in CAPS:
        if key not in seen:
            lines.append(f"{key}: {values[key]}\n")
    target.write_text("".join(lines), encoding="utf-8")
    return values


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
    """Stable ordering: the human's order first (`Approved.position`, lowest first), then
    breaking items (if preferred), then by policy.order."""

    def key(a: Approved):
        ordered = (0, a.position) if a.position is not None else (1, 0)
        breaking = 0 if (policy.prefer_breaking and is_breaking(a, policy.breaking)) else 1
        if policy.order == "oldest_first":
            return (*ordered, breaking, a.approved_at or "", a.draft_id)
        return (*ordered, breaking, -(a.score or 0.0), a.approved_at or "", a.draft_id)

    return sorted(approved, key=key)


def pick_for_slot(approved: list[Approved], slot: datetime, policy: Policy) -> Approved | None:
    """Best candidate for `slot`, or None when nothing is eligible or the caps block posting."""
    _require_aware(slot)
    if not approved or policy.blocked_reason(slot):
        return None
    return rank(approved, policy)[0]
