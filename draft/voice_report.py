"""Voice report: what the reviewer's edits and rejections are asking voice.md to change.

Pure functions (build_report, render_markdown, parse_banned_phrases) plus a small CLI:

    python -m draft.voice_report [--weeks N] [--out FILE] [--json] [--examples]

The report PROPOSES voice.md changes and applies none; the human edits draft/voice.md by
hand. It quotes only the human's own text and counts. The database is read only through
approval_queue.store adapters (fetch_draft_stats, fetch_decisions_for_voice); no network.
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import statistics
import sys
from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from draft.examples import (
    ACTION_EDIT,
    ACTION_REJECT,
    ACTION_REVISE,
    EditExample,
    _field,
    format_examples_block,
    parse_decision_text,
    select_edit_examples,
    select_rejections,
)
from draft.prompt import VOICE_PATH
from draft.settings import load_draft_config

log = logging.getLogger(__name__)

KIND_BANNED_PHRASE = "banned_phrase"
KIND_LENGTH = "length"
KIND_THREAD = "thread"
KIND_TONE = "tone"

LENGTH_DELTA_THRESHOLD = -40  # median chars removed from the single post -> "running long"
THREAD_DROPPED_THRESHOLD = 0.5
TONE_REPEATS = 3
TOP_WORDS = 15
MAX_PHRASE_WORDS = 3

DEFAULT_TONE_WORDS = ("hype", "hypey", "jargon", "vague", "wordy", "clickbait", "breathless")

SECTION_HEADINGS = (
    "## Summary",
    "## By source",
    "## By category",
    "## Length and threads",
    "## Banned phrase hits",
    "## Words the reviewer deletes and adds",
    "## Top edits",
    "## Rejection reasons",
    "## Proposals",
    "## Caveats",
)

CAVEATS = (
    "- Small n: a handful of edits is anecdote, not a trend. Read the counts before acting.",
    "- These edits reflect one reviewer's taste on the drafts they happened to see.",
    "- Hard rules (no medical advice, source URL, preprint label, verbatim numbers, 280 chars) "
    "are enforced in code by draft/drafter.py:check_hard_rules. They are never learned from "
    "examples and no proposal here changes them.",
    "- The report proposes; a human edits draft/voice.md by hand. Nothing is applied "
    "automatically.",
)

_URL_RE = re.compile(r"https?://\S+")
_WORD_RE = re.compile(r"[a-z][a-z'\-]*[a-z]|[a-z]")


@dataclass
class Proposal:
    kind: str  # one of KIND_*
    text: str  # what to paste where (draft/voice.md, which section)
    evidence: str


@dataclass
class VoiceReport:
    window_start: str
    window_end: str
    drafts_total: int
    approved_unedited: int
    edited: int
    rejected: int
    snoozed: int
    failed: int
    edit_rate: float | None
    by_source: list[tuple[str, int, int, int]] = field(default_factory=list)
    by_category: list[tuple[str, int]] = field(default_factory=list)
    length_delta_median: float | None = None
    thread_dropped_rate: float | None = None
    banned_phrase_hits: list[tuple[str, int]] = field(default_factory=list)
    deleted_words: list[tuple[str, int]] = field(default_factory=list)
    added_words: list[tuple[str, int]] = field(default_factory=list)
    top_pairs: list[EditExample] = field(default_factory=list)
    rejection_notes: list[tuple[str, int]] = field(default_factory=list)
    proposals: list[Proposal] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------------------
# voice.md parsing
# ---------------------------------------------------------------------------


def parse_banned_phrases(voice_md: str) -> list[str]:
    """Phrases from the '## Banned phrases' section of voice.md, lowercased.

    Each bullet may list several comma-separated phrases; quotes are stripped and the
    parenthetical "(unless ...)" tails are dropped. Bullets that quote phrases contribute only
    the quoted parts.
    """
    section: list[str] = []
    inside = False
    for line in voice_md.splitlines():
        if line.startswith("## "):
            inside = line.strip().lower() == "## banned phrases"
            continue
        if inside:
            section.append(line)
    phrases: list[str] = []
    for line in section:
        line = line.strip()
        if not line.startswith("- "):
            continue
        body = re.sub(r"\([^)]*\)", "", line[2:])
        quoted = re.findall(r"[\"“”']([^\"“”']+)[\"“”']", body)
        parts = quoted if quoted else body.split(",")
        for part in parts:
            phrase = part.strip().strip("\"“”' ").lower()
            if phrase and phrase not in phrases:
                phrases.append(phrase)
    return phrases


def _phrase_pattern(phrase: str) -> re.Pattern[str]:
    words = [re.escape(w) for w in re.split(r"[\s\-]+", phrase) if w]
    return re.compile(r"\b" + r"[\s\-]+".join(words) + r"\b", re.IGNORECASE)


# ---------------------------------------------------------------------------
# Text helpers
# ---------------------------------------------------------------------------


def words_of(text: str) -> list[str]:
    """Lowercased words; URLs and numbers removed."""
    return _WORD_RE.findall(_URL_RE.sub(" ", text.lower()))


def _ngrams(words: list[str], n: int) -> list[str]:
    return [" ".join(words[i : i + n]) for i in range(len(words) - n + 1)]


def _texts(single: str, thread: list[str]) -> str:
    return "\n".join([single, *thread])


def _rate(num: int, den: int) -> float | None:
    return None if den == 0 else num / den


def _top(counter: Counter[str], n: int) -> list[tuple[str, int]]:
    return sorted(counter.items(), key=lambda kv: (-kv[1], kv[0]))[:n]


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------


@dataclass
class _Edit:
    row: Any
    original: tuple[str, list[str]]
    edited: tuple[str, list[str]]


def _edits(decisions_rows: Iterable[Any]) -> list[_Edit]:
    out: list[_Edit] = []
    for r in decisions_rows:
        if _field(r, "action") != ACTION_EDIT:
            continue
        edited_text = _field(r, "edited_text")
        original_text = _field(r, "original_text", "")
        if not edited_text or edited_text == original_text:
            continue
        original, edited = parse_decision_text(original_text), parse_decision_text(edited_text)
        if original == edited:
            continue
        out.append(_Edit(r, original, edited))
    return out


def _deleted_phrases(edits: list[_Edit], stopwords: set[str]) -> Counter[str]:
    """Net count of 1-3 word phrases removed by the reviewer across all edits."""
    counts: Counter[str] = Counter()
    for e in edits:
        before = words_of(_texts(*e.original))
        after = words_of(_texts(*e.edited))
        for n in range(1, MAX_PHRASE_WORDS + 1):
            gone = Counter(_ngrams(before, n)) - Counter(_ngrams(after, n))
            for phrase, k in gone.items():
                if any(w in stopwords for w in phrase.split()):
                    continue
                counts[phrase] += k
    return counts


def _word_deltas(edits: list[_Edit], stopwords: set[str]) -> tuple[Counter[str], Counter[str]]:
    deleted: Counter[str] = Counter()
    added: Counter[str] = Counter()
    for e in edits:
        before = Counter(words_of(_texts(*e.original)))
        after = Counter(words_of(_texts(*e.edited)))
        for w, k in (before - after).items():
            if w not in stopwords:
                deleted[w] += k
        for w, k in (after - before).items():
            if w not in stopwords:
                added[w] += k
    return deleted, added


def _proposals(
    *,
    deleted_phrases: Counter[str],
    banned: list[str],
    length_delta_median: float | None,
    thread_dropped_rate: float | None,
    edits_n: int,
    notes: list[str],
    report_cfg: Mapping[str, Any],
) -> list[Proposal]:
    out: list[Proposal] = []
    threshold = int(report_cfg.get("propose_banned_after", 3))
    banned_set = set(banned)
    candidates = [
        (p, k) for p, k in deleted_phrases.items() if k >= threshold and p not in banned_set
    ]
    candidates.sort(key=lambda pk: (-len(pk[0].split()), -pk[1], pk[0]))
    chosen: list[str] = []
    for phrase, k in candidates:
        if any(f" {phrase} " in f" {longer} " for longer in chosen):
            continue  # already covered by a longer proposed phrase
        chosen.append(phrase)
        out.append(
            Proposal(
                KIND_BANNED_PHRASE,
                f'Add "- {phrase}" under "## Banned phrases" in draft/voice.md.',
                f'the reviewer deleted "{phrase}" {k} times in {edits_n} edits',
            )
        )
    if length_delta_median is not None and length_delta_median < LENGTH_DELTA_THRESHOLD:
        out.append(
            Proposal(
                KIND_LENGTH,
                'Posts are running long. Under "## Tone" in draft/voice.md add: '
                '"- Aim for well under the limit; cut the second idea, not the interpretation."',
                f"median single-post length change across {edits_n} edits was "
                f"{length_delta_median:+.0f} chars",
            )
        )
    if thread_dropped_rate is not None and thread_dropped_rate > THREAD_DROPPED_THRESHOLD:
        out.append(
            Proposal(
                KIND_THREAD,
                'Threads are being cut; lead with the single post. Under "## Tone" in '
                'draft/voice.md add: "- The single post carries the story; the thread only '
                'adds detail that does not fit, never restates it."',
                f"the reviewer shortened the thread in {thread_dropped_rate:.0%} of "
                f"{edits_n} edits",
            )
        )
    tone_words = [str(w).lower() for w in report_cfg.get("tone_words") or DEFAULT_TONE_WORDS]
    note_words: Counter[str] = Counter()
    for note in notes:
        for w in set(words_of(note)):
            if w in tone_words:
                note_words[w] += 1
    for w, k in _top(note_words, len(note_words)):
        if k >= TONE_REPEATS:
            out.append(
                Proposal(
                    KIND_TONE,
                    f'The reviewer keeps writing "{w}" in notes. Under "## Tone" in '
                    f"draft/voice.md add a line that names the pattern and the alternative, "
                    f'and consider a "## Sample posts" entry that shows it done right.',
                    f'"{w}" appears in {k} edit/reject notes',
                )
            )
    return out


def build_report(
    drafts_rows: Iterable[Any],
    decisions_rows: Iterable[Any],
    voice_md: str,
    cfg: Mapping[str, Any] | None,
    *,
    now: datetime | None = None,
    weeks: int | None = None,
) -> VoiceReport:
    """Summarise drafts and decisions from the last `weeks` weeks.

    drafts_rows come from store.fetch_draft_stats, decisions_rows from
    store.fetch_decisions_for_voice (both already filtered by the window start), voice_md is the
    text of draft/voice.md, cfg the full draft/config.yaml dict.
    """
    cfg = dict(cfg or {})
    report_cfg = dict(cfg.get("report") or {})
    examples_cfg = dict(cfg.get("examples") or {})
    now = now or datetime.now(UTC)
    weeks = int(weeks or report_cfg.get("weeks", 4))
    window_start = now - timedelta(weeks=weeks)
    start_text = window_start.replace(microsecond=0).isoformat()
    stopwords = {str(w).lower() for w in report_cfg.get("stopwords") or []}

    drafts = [r for r in drafts_rows if str(_field(r, "created_at", "")) >= start_text]
    decisions = [r for r in decisions_rows if str(_field(r, "created_at", "")) >= start_text]
    edits = _edits(decisions)

    edited_draft_ids = {int(_field(e.row, "draft_id", 0)) for e in edits}
    status_of = {int(_field(r, "id", 0)): str(_field(r, "status", "")) for r in drafts}
    source_of = {int(_field(r, "id", 0)): str(_field(r, "source", "") or "") for r in drafts}
    for r in decisions:
        source_of.setdefault(int(_field(r, "draft_id", 0)), str(_field(r, "source", "") or ""))

    drafts_total = len(drafts)
    edited = sum(1 for d in status_of if d in edited_draft_ids)
    approved_unedited = sum(
        1 for d, s in status_of.items() if s == "approved" and d not in edited_draft_ids
    )
    rejected = sum(1 for s in status_of.values() if s == "rejected")
    snoozed = sum(1 for s in status_of.values() if s == "snoozed")
    failed = sum(1 for s in status_of.values() if s == "failed")
    reviewed = approved_unedited + edited + rejected
    edit_rate = _rate(edited, reviewed)

    per_source: dict[str, list[int]] = {}
    for d, s in status_of.items():
        row = per_source.setdefault(source_of.get(d, "") or "?", [0, 0, 0])
        row[0] += 1
        row[1] += int(d in edited_draft_ids)
        row[2] += int(s == "rejected")
    by_source = sorted(
        ((src, n, e, r) for src, (n, e, r) in per_source.items()), key=lambda t: (-t[1], t[0])
    )

    categories: Counter[str] = Counter(
        str(_field(r, "category")) for r in decisions if _field(r, "category")
    )
    by_category = _top(categories, len(categories))

    deltas = [len(e.edited[0]) - len(e.original[0]) for e in edits]
    length_delta_median = statistics.median(deltas) if deltas else None
    thread_dropped_rate = _rate(
        sum(1 for e in edits if len(e.edited[1]) < len(e.original[1])), len(edits)
    )

    banned = parse_banned_phrases(voice_md)
    hits: Counter[str] = Counter()
    originals = [_texts(*parse_decision_text(_field(r, "original_text", ""))) for r in decisions]
    for phrase in banned:
        pat = _phrase_pattern(phrase)
        n = sum(1 for text in originals if pat.search(text))
        if n:
            hits[phrase] = n
    banned_phrase_hits = _top(hits, len(hits))

    deleted, added = _word_deltas(edits, stopwords)
    deleted_words = _top(deleted, TOP_WORDS)
    added_words = _top(added, TOP_WORDS)

    pair_cfg = {
        **examples_cfg,
        "lookback_days": weeks * 7,
        "max_examples": int(report_cfg.get("top_pairs", 10)),
    }
    top_pairs = select_edit_examples(decisions, pair_cfg, now=now)

    reasons: Counter[str] = Counter()
    for r in decisions:
        if _field(r, "action") != ACTION_REJECT:
            continue
        reason = str(_field(r, "note", "") or "").strip() or str(_field(r, "category", "") or "")
        if reason:
            reasons[reason] += 1
    rejection_notes = _top(reasons, len(reasons))

    notes = [
        str(_field(r, "note", "") or "")
        for r in decisions
        if _field(r, "action") in (ACTION_EDIT, ACTION_REJECT, ACTION_REVISE) and _field(r, "note")
    ]
    proposals = _proposals(
        deleted_phrases=_deleted_phrases(edits, stopwords),
        banned=banned,
        length_delta_median=length_delta_median,
        thread_dropped_rate=thread_dropped_rate,
        edits_n=len(edits),
        notes=notes,
        report_cfg=report_cfg,
    )

    report = VoiceReport(
        window_start=start_text,
        window_end=now.replace(microsecond=0).isoformat(),
        drafts_total=drafts_total,
        approved_unedited=approved_unedited,
        edited=edited,
        rejected=rejected,
        snoozed=snoozed,
        failed=failed,
        edit_rate=edit_rate,
        by_source=by_source,
        by_category=by_category,
        length_delta_median=length_delta_median,
        thread_dropped_rate=thread_dropped_rate,
        banned_phrase_hits=banned_phrase_hits,
        deleted_words=deleted_words,
        added_words=added_words,
        top_pairs=top_pairs,
        rejection_notes=rejection_notes,
        proposals=proposals,
    )
    log.info(
        "voice report: %d drafts, %d edited, %d rejected, %d proposals (last %d weeks)",
        drafts_total,
        edited,
        rejected,
        len(proposals),
        weeks,
    )
    return report


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def _pct(x: float | None) -> str:
    return "n/a" if x is None else f"{x:.0%}"


def _table(header: list[str], rows: list[list[Any]]) -> list[str]:
    out = ["| " + " | ".join(header) + " |", "|" + "|".join("---" for _ in header) + "|"]
    out.extend("| " + " | ".join(str(c) for c in r) + " |" for r in rows)
    return out


def render_markdown(report: VoiceReport) -> str:
    r = report
    lines = [
        "# Voice report",
        "",
        f"Window: {r.window_start} to {r.window_end}",
        "",
        SECTION_HEADINGS[0],
        "",
    ]
    lines += _table(
        ["Drafts", "Approved unedited", "Edited", "Rejected", "Snoozed", "Failed", "Edit rate"],
        [
            [
                r.drafts_total,
                r.approved_unedited,
                r.edited,
                r.rejected,
                r.snoozed,
                r.failed,
                _pct(r.edit_rate),
            ]
        ],
    )
    lines += [
        "",
        "Edit rate = edited / (approved unedited + edited + rejected).",
        "",
        SECTION_HEADINGS[1],
        "",
    ]
    if r.by_source:
        lines += _table(["Source", "Drafts", "Edited", "Rejected"], [list(t) for t in r.by_source])
    else:
        lines.append("_No drafts in the window._")
    lines += ["", SECTION_HEADINGS[2], ""]
    if r.by_category:
        lines += _table(["Category", "Decisions"], [list(t) for t in r.by_category])
    else:
        lines.append("_No categorised decisions yet (pick one in the queue's edit/reject forms)._")
    lines += ["", SECTION_HEADINGS[3], ""]
    if r.length_delta_median is None:
        lines.append("_No edits yet._")
    else:
        lines.append(
            f"- Median single-post length change: {r.length_delta_median:+.0f} chars "
            f"(negative = the reviewer shortens)."
        )
        lines.append(f"- Edits that shortened the thread: {_pct(r.thread_dropped_rate)}.")
    lines += ["", SECTION_HEADINGS[4], ""]
    if r.banned_phrase_hits:
        lines.append("Banned phrases (from voice.md) found in the model's original text:")
        lines.append("")
        lines += _table(["Phrase", "Drafts"], [list(t) for t in r.banned_phrase_hits])
    else:
        lines.append("_None found in the model's original text._")
    lines += ["", SECTION_HEADINGS[5], ""]
    if r.deleted_words or r.added_words:
        lines.append("Deleted: " + (", ".join(f"{w} ({n})" for w, n in r.deleted_words) or "none"))
        lines.append("")
        lines.append("Added: " + (", ".join(f"{w} ({n})" for w, n in r.added_words) or "none"))
    else:
        lines.append("_No edits yet._")
    lines += ["", SECTION_HEADINGS[6], ""]
    if r.top_pairs:
        for i, e in enumerate(r.top_pairs, 1):
            lines.append(f"### {i}. {e.source or '?'} · {e.created_at[:10]} · {e.why}")
            lines.append("")
            lines.append("Before:")
            lines.append("")
            lines.append("> " + e.original_single.replace("\n", "\n> "))
            lines.append("")
            lines.append("After:")
            lines.append("")
            lines.append("> " + e.edited_single.replace("\n", "\n> "))
            if e.thread_changed:
                lines.append("")
                lines.append(
                    f"Thread: {len(e.original_thread)} posts before, {len(e.edited_thread)} after."
                )
            lines.append("")
    else:
        lines.append("_No edits yet._")
        lines.append("")
    lines += [SECTION_HEADINGS[7], ""]
    if r.rejection_notes:
        lines += _table(["Reason", "Rejections"], [list(t) for t in r.rejection_notes])
    else:
        lines.append("_No rejection reasons recorded._")
    lines += ["", SECTION_HEADINGS[8], ""]
    if r.proposals:
        for p in r.proposals:
            lines.append(f"- **{p.kind}**: {p.text}")
            lines.append(f"  - evidence: {p.evidence}")
    else:
        lines.append("_Nothing to propose yet. That is the expected state early on._")
    lines += ["", SECTION_HEADINGS[9], ""]
    lines += list(CAVEATS)
    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _window_start(now: datetime, days: float) -> datetime:
    return now - timedelta(days=days)


def main(argv: list[str] | None = None) -> int:
    from dotenv import load_dotenv

    from approval_queue import store

    load_dotenv()
    cfg = load_draft_config()
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--weeks", type=int, default=None, help="window (default from config)")
    ap.add_argument("--out", type=Path, default=None, help="write the report here")
    ap.add_argument("--json", action="store_true", help="emit JSON instead of markdown")
    ap.add_argument(
        "--examples",
        action="store_true",
        help="print the exact examples block the next run_draft would send, then exit",
    )
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    now = datetime.now(UTC)
    conn = store.connect()
    try:
        if args.examples:
            ex_cfg = cfg["examples"]
            rows = store.fetch_decisions_for_voice(
                conn, _window_start(now, float(ex_cfg.get("lookback_days", 60)))
            )
            edits = select_edit_examples(rows, ex_cfg, now=now)
            rejections = select_rejections(rows, ex_cfg, now=now)
            block = format_examples_block(edits, rejections)
            log.info(
                "voice examples: %d edits, %d rejections (last %s days)",
                len(edits),
                len(rejections),
                ex_cfg.get("lookback_days", 60),
            )
            print(block if block else "(no examples qualify; the prompt is unchanged)")
            return 0
        weeks = int(args.weeks or cfg["report"].get("weeks", 4))
        since = now - timedelta(weeks=weeks)
        report = build_report(
            store.fetch_draft_stats(conn, since),
            store.fetch_decisions_for_voice(conn, since),
            VOICE_PATH.read_text(encoding="utf-8"),
            cfg,
            now=now,
            weeks=weeks,
        )
    finally:
        conn.close()
    text = json.dumps(report.to_dict(), indent=2) if args.json else render_markdown(report)
    if args.out:
        args.out.write_text(text, encoding="utf-8")
        log.info("wrote %s", args.out)
    else:
        print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
