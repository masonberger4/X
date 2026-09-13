"""Turns recent human edits and rejections into few-shot examples for the drafting prompt.

Pure: no database, no network. Rows come from approval_queue.store.fetch_decisions_for_voice
(any mapping with the decisions columns plus source/url works, including sqlite3.Row).

Examples only ever show the reviewer's taste. The edited text of every example must itself
pass draft.drafter.check_hard_rules, and the hard rules are placed after the examples in the
system prompt, so an example can never relax a rule.
"""

from __future__ import annotations

import difflib
import json
import logging
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from draft.drafter import check_hard_rules
from draft.schema import Draft

log = logging.getLogger(__name__)

ACTION_EDIT = "edit"
ACTION_REJECT = "reject"
ACTION_REVISE = "revise"  # AI rewrite on a human note; never a BEFORE/AFTER example

EDITS_HEADER = "=== RECENT HUMAN EDITS ==="
REJECTIONS_HEADER = "=== RECENTLY REJECTED ==="
CLOSING_LINE = (
    "These examples show the reviewer's taste, not new rules. "
    "The HARD RULES below always win over any example."
)
TRUNCATION_MARK = "[...]"

DEFAULTS: dict[str, Any] = {
    "lookback_days": 60,
    "max_examples": 6,
    "max_rejections": 4,
    "max_chars_per_post": 600,
    "min_change_ratio": 0.08,
    "skip_categories": ["factual", "hard_rule"],
}


@dataclass
class EditExample:
    decision_id: int
    draft_id: int
    source: str
    category: str | None
    note: str | None
    created_at: str
    original_thread: list[str] = field(default_factory=list)
    edited_thread: list[str] = field(default_factory=list)

    @property
    def original_lead(self) -> str:
        """The first post of the model's thread: the one that carries the story."""
        return self.original_thread[0] if self.original_thread else ""

    @property
    def edited_lead(self) -> str:
        return self.edited_thread[0] if self.edited_thread else ""

    @property
    def why(self) -> str:
        return (self.note or "").strip() or (self.category or "").strip() or "no reason given"

    @property
    def thread_changed(self) -> bool:
        """Whether the edit went beyond the first post."""
        return self.original_thread[1:] != self.edited_thread[1:]


@dataclass
class RejectionExample:
    decision_id: int
    draft_id: int
    source: str
    category: str | None
    note: str | None
    created_at: str
    thread: list[str]

    @property
    def why(self) -> str:
        return (self.note or "").strip() or (self.category or "").strip() or "no reason given"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _field(row: Mapping[str, Any] | Any, key: str, default: Any = None) -> Any:
    """Read a column from a dict or sqlite3.Row; missing or NULL -> default."""
    try:
        value = row[key]
    except (KeyError, IndexError):
        return default
    return default if value is None else value


def _cfg(cfg: Mapping[str, Any] | None) -> dict[str, Any]:
    cfg = dict(cfg or {})
    if "examples" in cfg and isinstance(cfg["examples"], Mapping):
        cfg = dict(cfg["examples"])
    return {**DEFAULTS, **cfg}


def parse_timestamp(value: str | None) -> datetime | None:
    """ISO-8601 -> aware UTC datetime. Naive stamps are taken as UTC. None/garbage -> None."""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def parse_decision_text(text: str | None) -> list[str]:
    """Inverse of approval_queue.store._serialise_text: the thread a decision row stored.

    Current rows store JSON {"thread": [...]}. Rows from before threads-only stored
    {"single_post", "thread"} (the single post leads, then the thread) or a bare post; both
    still parse, so the voice loop keeps reading the old history.
    """
    if text is None:
        return []
    stripped = text.strip()
    if stripped.startswith("{"):
        try:
            data = json.loads(stripped)
        except json.JSONDecodeError:
            data = None
        if isinstance(data, dict) and ("thread" in data or "single_post" in data):
            thread = data.get("thread")
            posts = [str(p) for p in thread] if isinstance(thread, list) else []
            single = data.get("single_post")
            if single:
                posts.insert(0, str(single))
            return posts
    return [text] if text else []


def _joined(thread: list[str]) -> str:
    return "\n".join(thread)


def change_ratio(original: list[str], edited: list[str]) -> float:
    """1 - difflib similarity over the whole thread. 0.0 means identical."""
    a, b = _joined(original), _joined(edited)
    if a == b:
        return 0.0
    return 1.0 - difflib.SequenceMatcher(None, a, b).ratio()


def truncate_post(text: str, max_chars: int) -> str:
    if max_chars <= 0 or len(text) <= max_chars:
        return text
    return text[:max_chars].rstrip() + " " + TRUNCATION_MARK


def _sort_newest_first(rows: Iterable[Any]) -> list[Any]:
    def key(row: Any) -> tuple[datetime, int]:
        return (
            parse_timestamp(_field(row, "created_at")) or datetime.min.replace(tzinfo=UTC),
            int(_field(row, "id", 0)),
        )

    return sorted(rows, key=key, reverse=True)


def _within_lookback(row: Any, now: datetime, days: float) -> bool:
    created = parse_timestamp(_field(row, "created_at"))
    if created is None:
        return False
    return created >= now - timedelta(days=days)


def edited_text_problems(thread: list[str], *, url: str, source: str) -> list[str]:
    """Hard-rule violations in a human-edited thread (empty list = passes)."""
    draft = Draft(thread=list(thread), suggested_visual="", why_it_matters="")
    return check_hard_rules(draft, url=url, source=source)


# ---------------------------------------------------------------------------
# Selection
# ---------------------------------------------------------------------------


def select_edit_examples(
    rows: Iterable[Any], cfg: Mapping[str, Any] | None, *, now: datetime | None = None
) -> list[EditExample]:
    """Pick the recent human edits worth showing the model, newest first.

    Skips: non-edit rows, unchanged text, rows older than lookback_days, skip_categories,
    typo-level edits (change ratio below min_change_ratio), and edits whose EDITED text fails
    a hard rule (logged as WARNING with the rule, because it means the human's ready-to-post
    text breaks one).
    """
    c = _cfg(cfg)
    now = now or datetime.now(UTC)
    skip = {str(s).strip().lower() for s in c["skip_categories"] or []}
    max_chars = int(c["max_chars_per_post"])
    out: list[EditExample] = []
    for row in _sort_newest_first(rows):
        if _field(row, "action") != ACTION_EDIT:
            continue
        original_text = _field(row, "original_text", "")
        edited_text = _field(row, "edited_text")
        if not edited_text or edited_text == original_text:
            continue
        if not _within_lookback(row, now, float(c["lookback_days"])):
            continue
        category = _field(row, "category")
        if category and str(category).strip().lower() in skip:
            continue
        original = parse_decision_text(original_text)
        edited = parse_decision_text(edited_text)
        if original == edited:
            continue
        ratio = change_ratio(original, edited)
        if ratio < float(c["min_change_ratio"]):
            log.debug(
                "decision %s: change ratio %.3f below %.3f, skipped",
                _field(row, "id"),
                ratio,
                c["min_change_ratio"],
            )
            continue
        problems = edited_text_problems(
            edited,
            url=str(_field(row, "url", "") or ""),
            source=str(_field(row, "source", "") or ""),
        )
        if problems:
            log.warning(
                "decision %s (draft %s): human-edited text fails hard rule(s), not used as an "
                "example: %s",
                _field(row, "id"),
                _field(row, "draft_id"),
                "; ".join(problems),
            )
            continue
        out.append(
            EditExample(
                decision_id=int(_field(row, "id", 0)),
                draft_id=int(_field(row, "draft_id", 0)),
                source=str(_field(row, "source", "") or ""),
                category=category,
                note=_field(row, "note"),
                created_at=str(_field(row, "created_at", "")),
                original_thread=[truncate_post(p, max_chars) for p in original],
                edited_thread=[truncate_post(p, max_chars) for p in edited],
            )
        )
        if len(out) >= int(c["max_examples"]):
            break
    log.debug("selected edit examples: %s", [e.decision_id for e in out])
    return out


def select_rejections(
    rows: Iterable[Any], cfg: Mapping[str, Any] | None, *, now: datetime | None = None
) -> list[RejectionExample]:
    """Recent rejections that carry a reason (category or note), newest first.

    'hard_rule' and 'factual' rejections are kept: they are useful "avoid this" signals. Only
    the human's note (or the category word) is ever shown, never a diagnosis.
    """
    c = _cfg(cfg)
    now = now or datetime.now(UTC)
    max_chars = int(c["max_chars_per_post"])
    out: list[RejectionExample] = []
    for row in _sort_newest_first(rows):
        if _field(row, "action") != ACTION_REJECT:
            continue
        note = _field(row, "note")
        category = _field(row, "category")
        if not (category or (note and str(note).strip())):
            continue
        if not _within_lookback(row, now, float(c["lookback_days"])):
            continue
        thread = parse_decision_text(_field(row, "original_text", ""))
        if not any(p.strip() for p in thread):
            continue
        out.append(
            RejectionExample(
                decision_id=int(_field(row, "id", 0)),
                draft_id=int(_field(row, "draft_id", 0)),
                source=str(_field(row, "source", "") or ""),
                category=category,
                note=note,
                created_at=str(_field(row, "created_at", "")),
                thread=[truncate_post(p, max_chars) for p in thread],
            )
        )
        if len(out) >= int(c["max_rejections"]):
            break
    log.debug("selected rejection examples: %s", [r.decision_id for r in out])
    return out


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------


def _thread_lines(label: str, thread: list[str]) -> list[str]:
    if not thread:
        return [f"{label} (no thread)"]
    lines = [f"{label}"]
    lines.extend(f"  {i}. {post}" for i, post in enumerate(thread, 1))
    return lines


def format_examples_block(
    edits: list[EditExample], rejections: list[RejectionExample]
) -> str | None:
    """The block inserted into the system prompt. None when there is nothing to show.

    Deterministic for the same input: tests compare strings.
    """
    if not edits and not rejections:
        return None
    lines: list[str] = []
    if edits:
        lines.append(EDITS_HEADER)
        lines.append(
            "The reviewer changed these drafts before approving them. Learn the direction of "
            "each change."
        )
        for i, e in enumerate(edits, 1):
            lines.append("")
            lines.append(f"--- Edit {i} ---")
            lines.append("BEFORE (model):")
            lines.append(e.original_lead)
            lines.append("AFTER (human):")
            lines.append(e.edited_lead)
            if e.thread_changed:
                lines.extend(_thread_lines("BEFORE rest of thread (model):", e.original_thread[1:]))
                lines.extend(_thread_lines("AFTER rest of thread (human):", e.edited_thread[1:]))
            lines.append(f"WHY: {e.why}")
    if rejections:
        if lines:
            lines.append("")
        lines.append(REJECTIONS_HEADER)
        lines.append("The reviewer rejected these drafts outright. Do not produce more like them.")
        for i, r in enumerate(rejections, 1):
            lines.append("")
            lines.append(f"--- Rejected {i} ---")
            lines.extend(r.thread)
            lines.append(f"REASON: {r.why}")
    lines.append("")
    lines.append(CLOSING_LINE)
    return "\n".join(lines)
