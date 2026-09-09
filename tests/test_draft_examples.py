"""Pure tests for draft.examples: selection, filtering, truncation, formatting."""

import json
import logging
from datetime import UTC, datetime, timedelta

from approval_queue.store import _serialise_text
from draft.examples import (
    CLOSING_LINE,
    EDITS_HEADER,
    REJECTIONS_HEADER,
    TRUNCATION_MARK,
    EditExample,
    RejectionExample,
    change_ratio,
    format_examples_block,
    parse_decision_text,
    select_edit_examples,
    select_rejections,
)

URL = "https://doi.org/10.1000/xyz123"
NOW = datetime(2026, 9, 9, 12, 0, tzinfo=UTC)
CFG = {
    "lookback_days": 60,
    "max_examples": 6,
    "max_rejections": 4,
    "max_chars_per_post": 600,
    "min_change_ratio": 0.08,
    "skip_categories": ["factual", "hard_rule"],
}

ORIG_SINGLE = f"A game-changing CAR-T result: ORR 88% in 97 patients. Exciting times. {URL}"
ORIG_THREAD = ["ORR 88% in 97 patients.", "Single arm, no comparator.", f"Source: {URL}"]
EDIT_SINGLE = f"ORR 88% in 97 patients, single-arm. Sequencing vs bispecifics is the question {URL}"
EDIT_THREAD = ["ORR 88% in 97 patients.", "Single arm, no comparator.", f"Source: {URL}"]

_ids = iter(range(1, 10_000))


def row(
    action="edit",
    *,
    original=(ORIG_SINGLE, ORIG_THREAD),
    edited=(EDIT_SINGLE, EDIT_THREAD),
    note="less hype",
    category=None,
    days_ago=1,
    source="pubmed",
    url=URL,
    decision_id=None,
    draft_id=7,
):
    """A dict shaped like a fetch_decisions_for_voice row."""
    if action == "edit":
        original_text = _serialise_text(*original)
        edited_text = _serialise_text(*edited) if edited is not None else None
    else:
        original_text = original[0]
        edited_text = None
    return {
        "id": decision_id if decision_id is not None else next(_ids),
        "draft_id": draft_id,
        "action": action,
        "original_text": original_text,
        "edited_text": edited_text,
        "note": note,
        "category": category,
        "created_at": (NOW - timedelta(days=days_ago)).isoformat(),
        "source": source,
        "url": url,
        "draft_status": "approved",
        "draft_created_at": (NOW - timedelta(days=days_ago + 1)).isoformat(),
    }


# --- parse_decision_text -----------------------------------------------------


def test_parse_decision_text_round_trips_serialise_text():
    text = _serialise_text("single ✓", ["one", "two"])
    assert parse_decision_text(text) == ("single ✓", ["one", "two"])


def test_parse_decision_text_accepts_bare_string_and_odd_json():
    assert parse_decision_text("just a post " + URL) == ("just a post " + URL, [])
    assert parse_decision_text("") == ("", [])
    assert parse_decision_text(None) == ("", [])
    assert parse_decision_text('{"other": 1}') == ('{"other": 1}', [])
    assert parse_decision_text("{not json") == ("{not json", [])
    assert parse_decision_text(json.dumps({"single_post": "s", "thread": None})) == ("s", [])


# --- select_edit_examples ------------------------------------------------------


def test_selects_a_real_edit_with_texts_and_reason():
    out = select_edit_examples([row(note="less hype", category="voice")], CFG, now=NOW)
    assert len(out) == 1
    e = out[0]
    assert isinstance(e, EditExample)
    assert e.original_single == ORIG_SINGLE and e.edited_single == EDIT_SINGLE
    assert e.original_thread == ORIG_THREAD and e.edited_thread == EDIT_THREAD
    assert e.category == "voice" and e.note == "less hype" and e.source == "pubmed"
    assert e.why == "less hype"
    assert not e.thread_changed


def test_identical_edit_is_skipped():
    same = (ORIG_SINGLE, ORIG_THREAD)
    assert select_edit_examples([row(original=same, edited=same)], CFG, now=NOW) == []
    assert select_edit_examples([row(edited=None)], CFG, now=NOW) == []


def test_one_character_edit_is_skipped_by_min_change_ratio():
    typo = (ORIG_SINGLE.replace("Exciting", "Excitng"), ORIG_THREAD)
    r = row(original=typo, edited=(ORIG_SINGLE, ORIG_THREAD))
    assert change_ratio(typo, (ORIG_SINGLE, ORIG_THREAD)) < 0.08
    assert select_edit_examples([r], CFG, now=NOW) == []
    # with the threshold lowered the same row qualifies
    assert len(select_edit_examples([r], {**CFG, "min_change_ratio": 0.0}, now=NOW)) == 1


def test_factual_and_hard_rule_edits_are_skipped():
    assert select_edit_examples([row(category="factual")], CFG, now=NOW) == []
    assert select_edit_examples([row(category="hard_rule")], CFG, now=NOW) == []
    assert len(select_edit_examples([row(category="voice")], CFG, now=NOW)) == 1
    assert len(select_edit_examples([row(category=None)], CFG, now=NOW)) == 1


def test_old_edits_are_skipped():
    assert select_edit_examples([row(days_ago=61)], CFG, now=NOW) == []
    assert len(select_edit_examples([row(days_ago=59)], CFG, now=NOW)) == 1


def test_non_edit_rows_are_ignored():
    rows = [row("approve"), row("reject", note="bad"), row("snooze")]
    assert select_edit_examples(rows, CFG, now=NOW) == []


def test_newest_first_and_capped_at_max_examples():
    rows = [row(days_ago=d, decision_id=100 + d) for d in (5, 1, 3, 2, 4)]
    out = select_edit_examples(rows, {**CFG, "max_examples": 3}, now=NOW)
    assert [e.decision_id for e in out] == [101, 102, 103]


def test_edited_text_that_breaks_a_hard_rule_is_never_taught(caplog):
    advice = (f"Patients should ask their oncologist about this. {URL}", EDIT_THREAD)
    no_url = ("Dropped the link entirely, oops.", ["a", "b", "c"])
    with caplog.at_level(logging.WARNING, logger="draft.examples"):
        out = select_edit_examples([row(edited=advice), row(edited=no_url)], CFG, now=NOW)
    assert out == []
    assert "medical advice" in caplog.text
    assert "missing the primary source URL" in caplog.text


def test_edited_text_dropping_the_thread_is_allowed_when_single_passes():
    out = select_edit_examples([row(edited=(EDIT_SINGLE, []))], CFG, now=NOW)
    assert len(out) == 1 and out[0].edited_thread == [] and out[0].thread_changed


def test_preprint_edit_must_keep_label():
    unlabelled = row(source="biorxiv", edited=(EDIT_SINGLE, EDIT_THREAD))
    assert select_edit_examples([unlabelled], CFG, now=NOW) == []
    labelled = row(
        source="biorxiv",
        edited=(f"Preprint: {EDIT_SINGLE}", [f"Preprint. {EDIT_THREAD[0]}", "b", f"c {URL}"]),
    )
    assert len(select_edit_examples([labelled], CFG, now=NOW)) == 1


def test_long_posts_are_truncated_with_visible_marker():
    long_single = "y" * 700 + " " + URL
    r = row(original=(long_single, ORIG_THREAD), edited=(EDIT_SINGLE, EDIT_THREAD))
    out = select_edit_examples([r], {**CFG, "max_chars_per_post": 100}, now=NOW)
    assert len(out) == 1
    assert out[0].original_single.endswith(TRUNCATION_MARK)
    assert len(out[0].original_single) <= 100 + len(TRUNCATION_MARK) + 1


def test_rows_work_with_sqlite_row_objects(tmp_path):
    import sqlite3

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE t (id, draft_id, action, original_text, edited_text, note, category,"
        " created_at, source, url)"
    )
    r = row()
    conn.execute(
        "INSERT INTO t VALUES (:id, :draft_id, :action, :original_text, :edited_text, :note,"
        " :category, :created_at, :source, :url)",
        r,
    )
    rows = conn.execute("SELECT * FROM t").fetchall()
    assert len(select_edit_examples(rows, CFG, now=NOW)) == 1


# --- select_rejections ---------------------------------------------------------


def test_rejections_need_a_reason_and_are_capped_newest_first():
    rows = [
        row("reject", note="", category=None, days_ago=1),  # no reason -> skipped
        row("reject", note="too hypey", days_ago=2, decision_id=202),
        row("reject", note=None, category="hard_rule", days_ago=3, decision_id=203),
        row("reject", note="not news", days_ago=4, decision_id=204),
        row("reject", note="old", days_ago=90, decision_id=290),
        row("edit", note="x"),
    ]
    out = select_rejections(rows, {**CFG, "max_rejections": 2}, now=NOW)
    assert [r.decision_id for r in out] == [202, 203]
    assert isinstance(out[0], RejectionExample)
    assert out[0].single_post == ORIG_SINGLE and out[0].why == "too hypey"
    assert out[1].why == "hard_rule"
    assert [r.decision_id for r in select_rejections(rows, CFG, now=NOW)] == [202, 203, 204]


# --- format_examples_block -----------------------------------------------------


def test_block_is_none_when_nothing_qualifies():
    assert format_examples_block([], []) is None
    assert format_examples_block(select_edit_examples([], CFG, now=NOW), []) is None


def test_block_is_deterministic_and_has_before_after_why():
    edits = select_edit_examples([row(note="less hype")], CFG, now=NOW)
    rejections = select_rejections([row("reject", note="not newsworthy")], CFG, now=NOW)
    block = format_examples_block(edits, rejections)
    assert block == format_examples_block(edits, rejections)
    assert block.startswith(EDITS_HEADER)
    assert "BEFORE (model):\n" + ORIG_SINGLE in block
    assert "AFTER (human):\n" + EDIT_SINGLE in block
    assert "WHY: less hype" in block
    assert REJECTIONS_HEADER in block
    assert "REASON: not newsworthy" in block
    assert block.endswith(CLOSING_LINE)
    assert block.index(EDITS_HEADER) < block.index(REJECTIONS_HEADER) < block.index(CLOSING_LINE)


def test_unchanged_thread_is_omitted_and_changed_thread_is_shown():
    same = select_edit_examples([row()], CFG, now=NOW)
    block = format_examples_block(same, [])
    assert "thread" not in block.lower().split("why:")[0]
    changed = select_edit_examples(
        [row(edited=(EDIT_SINGLE, ["Shorter.", "Two.", f"Source: {URL}"]))], CFG, now=NOW
    )
    block = format_examples_block(changed, [])
    assert "BEFORE thread (model):" in block and "AFTER thread (human):" in block
    assert "  1. Shorter." in block


def test_block_with_only_rejections_or_only_edits():
    rejections = select_rejections([row("reject", note=None, category="voice")], CFG, now=NOW)
    block = format_examples_block([], rejections)
    assert EDITS_HEADER not in block and block.startswith(REJECTIONS_HEADER)
    assert "REASON: voice" in block
    edits = select_edit_examples([row(note=None, category=None)], CFG, now=NOW)
    block = format_examples_block(edits, [])
    assert REJECTIONS_HEADER not in block and "WHY: no reason given" in block
