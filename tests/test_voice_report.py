"""Voice report tests with hand-built rows (pure) plus the CLI against a temp DB."""

import json
from datetime import UTC, datetime, timedelta

from approval_queue.store import _serialise_text
from draft.settings import load_draft_config
from draft.voice_report import (
    CAVEATS,
    KIND_BANNED_PHRASE,
    KIND_LENGTH,
    KIND_THREAD,
    KIND_TONE,
    SECTION_HEADINGS,
    VoiceReport,
    build_report,
    main,
    parse_banned_phrases,
    render_markdown,
    words_of,
)

URL = "https://doi.org/10.1000/xyz123"
NOW = datetime(2026, 9, 9, 12, 0, tzinfo=UTC)
CFG = load_draft_config()

VOICE_MD = """# Voice guide

## Tone
- Plain English.

## Banned phrases
- game-changer, game changing
- breakthrough (unless quoting an FDA Breakthrough Therapy designation verbatim)
- "should ask their doctor", "talk to your oncologist", "patients should"
- emojis

## Something else
- not a banned phrase
"""

_ids = iter(range(1, 10_000))


def draft_row(draft_id, *, source="pubmed", status="approved", days_ago=2):
    return {
        "id": draft_id,
        "item_id": f"item{draft_id}",
        "source": source,
        "status": status,
        "created_at": (NOW - timedelta(days=days_ago)).isoformat(),
        "model": "m",
    }


def decision_row(
    draft_id,
    action="edit",
    *,
    original=(f"A game-changer for CAR-T: ORR 88%. Exciting stuff. {URL}", ["t1", f"t2 {URL}"]),
    edited=(f"ORR 88%, single arm. Sequencing is the open question. {URL}", ["t1", f"t2 {URL}"]),
    note=None,
    category=None,
    days_ago=1,
    source="pubmed",
):
    if action == "edit":
        original_text, edited_text = _serialise_text(*original), _serialise_text(*edited)
    else:
        original_text, edited_text = original[0], None
    return {
        "id": next(_ids),
        "draft_id": draft_id,
        "action": action,
        "original_text": original_text,
        "edited_text": edited_text,
        "note": note,
        "category": category,
        "created_at": (NOW - timedelta(days=days_ago)).isoformat(),
        "draft_status": "approved",
        "draft_created_at": (NOW - timedelta(days=days_ago + 1)).isoformat(),
        "item_id": f"item{draft_id}",
        "source": source,
        "url": URL,
    }


def report(drafts, decisions, **kw):
    return build_report(drafts, decisions, VOICE_MD, CFG, now=NOW, weeks=4, **kw)


# --- parsing ------------------------------------------------------------------


def test_parse_banned_phrases_strips_quotes_and_unless_tails():
    phrases = parse_banned_phrases(VOICE_MD)
    assert phrases == [
        "game-changer",
        "game changing",
        "breakthrough",
        "should ask their doctor",
        "talk to your oncologist",
        "patients should",
        "emojis",
    ]
    assert parse_banned_phrases("# no section\n- game-changer\n") == []


def test_words_of_drops_urls_and_numbers():
    assert words_of(f"ORR 88% in 97 patients, see {URL} and 14.6 months") == [
        "orr",
        "in",
        "patients",
        "see",
        "and",
        "months",
    ]


# --- counts -------------------------------------------------------------------


def test_zero_drafts_gives_rate_none_and_no_division_error():
    r = report([], [])
    assert isinstance(r, VoiceReport)
    assert r.drafts_total == 0 and r.edit_rate is None
    assert r.length_delta_median is None and r.thread_dropped_rate is None
    assert r.proposals == [] and r.top_pairs == [] and r.banned_phrase_hits == []


def test_status_counts_edit_rate_and_by_source():
    drafts = [
        draft_row(1, status="approved"),
        draft_row(2, status="approved"),
        draft_row(3, status="rejected", source="fda"),
        draft_row(4, status="snoozed"),
        draft_row(5, status="failed"),
        draft_row(6, status="pending"),
        draft_row(7, status="approved", days_ago=60),  # outside the window
    ]
    decisions = [
        decision_row(1, "edit", note="less hype", category="voice"),
        decision_row(2, "approve"),
        decision_row(3, "reject", note="not news", category="not_newsworthy", source="fda"),
    ]
    r = report(drafts, decisions)
    assert r.drafts_total == 6
    assert (r.approved_unedited, r.edited, r.rejected, r.snoozed, r.failed) == (1, 1, 1, 1, 1)
    assert r.edit_rate == 1 / 3
    assert r.by_source == [("pubmed", 5, 1, 0), ("fda", 1, 0, 1)]
    assert r.by_category == [("not_newsworthy", 1), ("voice", 1)]
    assert r.rejection_notes == [("not news", 1)]
    assert r.window_start.startswith("2026-08-12") and r.window_end.startswith("2026-09-09")


def test_banned_phrase_hits_found_in_original_text():
    decisions = [
        decision_row(1, "edit"),  # original says "game-changer"
        decision_row(2, "reject", original=(f"Breakthrough! Truly. {URL}", []), note="hype"),
        decision_row(3, "approve", original=(f"Nothing banned here. {URL}", [])),
    ]
    r = report([draft_row(i) for i in (1, 2, 3)], decisions)
    assert r.banned_phrase_hits == [("breakthrough", 1), ("game-changer", 1)]


def test_deleted_and_added_words_exclude_stopwords_numbers_and_urls():
    original = (f"The result is exciting and huge for the 97 patients {URL}", [])
    edited = (f"The result is modest for the 97 patients https://other.example/x {URL}", [])
    r = report([draft_row(1)], [decision_row(1, original=original, edited=edited)])
    deleted = dict(r.deleted_words)
    added = dict(r.added_words)
    assert deleted == {"exciting": 1, "huge": 1}
    assert added == {"modest": 1}
    for words in (deleted, added):
        assert not any(w in ("the", "and", "for", "is") for w in words)
        assert not any(w[0].isdigit() or "http" in w for w in words)


def test_length_delta_median_and_thread_dropped_rate():
    long = "x" * 200 + " " + URL
    short = "x" * 140 + " " + URL
    decisions = [
        decision_row(1, original=(long, ["a", "b", f"c {URL}"]), edited=(short, [f"c {URL}"])),
        decision_row(2, original=(long, ["a", f"b {URL}"]), edited=(short, ["a", f"b {URL}"])),
    ]
    r = report([draft_row(1), draft_row(2)], decisions)
    assert r.length_delta_median == -60
    assert r.thread_dropped_rate == 0.5


# --- proposals ----------------------------------------------------------------


def _phrase_edits(n, phrase="paradigm shift"):
    return [
        decision_row(
            i,
            original=(f"A {phrase} in myeloma care, honestly. Result {i}. {URL}", []),
            edited=(f"A real change in myeloma care, honestly. Result {i}. {URL}", []),
        )
        for i in range(1, n + 1)
    ]


def test_phrase_deleted_three_times_is_proposed_twice_is_not():
    r = report([draft_row(i) for i in (1, 2)], _phrase_edits(2))
    assert not [p for p in r.proposals if p.kind == KIND_BANNED_PHRASE]
    r = report([draft_row(i) for i in (1, 2, 3)], _phrase_edits(3))
    banned = [p for p in r.proposals if p.kind == KIND_BANNED_PHRASE]
    assert len(banned) == 1
    assert banned[0].text == 'Add "- paradigm shift" under "## Banned phrases" in draft/voice.md.'
    assert "3 times" in banned[0].evidence
    # the sub-words "paradigm" / "shift" are covered by the longer phrase, not proposed twice
    assert not any(
        'paradigm"' in p.text or 'shift"' in p.text for p in banned if p is not banned[0]
    )


def test_already_banned_phrase_is_not_proposed_again():
    r = report([draft_row(i) for i in (1, 2, 3)], _phrase_edits(3, phrase="game-changer"))
    assert not [p for p in r.proposals if p.kind == KIND_BANNED_PHRASE]
    assert r.banned_phrase_hits == [("game-changer", 3)]


def test_length_proposal_from_median_delta_minus_60():
    long = "y" * 220 + " " + URL
    short = "y" * 160 + " " + URL
    decisions = [decision_row(i, original=(long, []), edited=(short, [])) for i in (1, 2, 3)]
    r = report([draft_row(i) for i in (1, 2, 3)], decisions)
    assert r.length_delta_median == -60
    lp = [p for p in r.proposals if p.kind == KIND_LENGTH]
    assert len(lp) == 1 and "running long" in lp[0].text and "draft/voice.md" in lp[0].text
    assert "-60" in lp[0].evidence

    # a -20 median does not
    shorter = "y" * 200 + " " + URL
    decisions = [decision_row(i, original=(long, []), edited=(shorter, [])) for i in (1, 2, 3)]
    r = report([draft_row(i) for i in (1, 2, 3)], decisions)
    assert not [p for p in r.proposals if p.kind == KIND_LENGTH]


def test_thread_proposal_when_threads_are_usually_cut():
    thread = ["a", "b", f"c {URL}"]
    decisions = [
        decision_row(
            i,
            original=(f"Original wording of the post {i}. {URL}", thread),
            edited=(f"Edited wording of the post {i}. {URL}", [f"c {URL}"]),
        )
        for i in (1, 2, 3)
    ]
    r = report([draft_row(i) for i in (1, 2, 3)], decisions)
    assert r.thread_dropped_rate == 1.0
    tp = [p for p in r.proposals if p.kind == KIND_THREAD]
    assert len(tp) == 1 and "lead with the single post" in tp[0].text


def test_tone_proposal_from_recurring_note_word():
    decisions = [
        decision_row(1, note="too much hype"),
        decision_row(2, "reject", note="Hype again", original=(f"x {URL}", [])),
        decision_row(3, note="hype, and jargon"),
        decision_row(4, note="jargon"),
    ]
    r = report([draft_row(i) for i in (1, 2, 3, 4)], decisions)
    tone = [p for p in r.proposals if p.kind == KIND_TONE]
    assert len(tone) == 1 and '"hype"' in tone[0].text and "3 edit/reject notes" in tone[0].evidence


def test_top_pairs_use_example_selection():
    decisions = [decision_row(1, note="less hype"), decision_row(2, category="factual")]
    r = report([draft_row(1), draft_row(2)], decisions)
    assert [e.draft_id for e in r.top_pairs] == [1]
    assert r.top_pairs[0].why == "less hype"


# --- rendering ----------------------------------------------------------------


def test_no_edits_state_renders_with_every_heading_and_caveats():
    md = render_markdown(report([], []))
    for heading in SECTION_HEADINGS:
        assert heading in md
    for caveat in CAVEATS:
        assert caveat in md
    assert "No edits yet" in md and "n/a" in md
    assert "Nothing to propose yet" in md


def test_markdown_with_data_contains_pairs_and_proposals():
    long = "y" * 220 + " " + URL
    short = "y" * 160 + " " + URL
    decisions = [
        decision_row(i, original=(long, []), edited=(short, []), note="tighten") for i in (1, 2, 3)
    ]
    r = report([draft_row(i) for i in (1, 2, 3)], decisions)
    md = render_markdown(r)
    for heading in SECTION_HEADINGS:
        assert heading in md
    assert "**length**" in md and "> " + short in md and "tighten" in md
    assert "| pubmed | 3 | 3 | 0 |" in md
    assert "Edit rate" in md and "100%" in md


def test_report_to_dict_is_json_serialisable():
    r = report([draft_row(1)], [decision_row(1, note="n")])
    data = json.loads(json.dumps(r.to_dict()))
    assert data["drafts_total"] == 1 and data["top_pairs"][0]["note"] == "n"


# --- CLI ----------------------------------------------------------------------


def test_cli_json_and_markdown_and_examples(db_file, capsys, tmp_path):
    from approval_queue import store
    from draft.schema import Draft
    from tests.conftest import seed_item

    conn = store.connect(db_file)
    seed_item(conn, "i1")
    did = store.insert_draft(
        conn,
        item_id="i1",
        model="m",
        draft=Draft(f"Huge, exciting CAR-T news today {URL}", ["a", f"b {URL}"], "", ""),
    )
    store.edit(
        conn, did, single_post=f"CAR-T data {URL}", thread=["a", f"b {URL}"], note="less hype"
    )
    conn.close()

    assert main(["--json", "--weeks", "2"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["drafts_total"] == 1 and data["edited"] == 1
    assert data["top_pairs"][0]["edited_single"] == f"CAR-T data {URL}"

    out = tmp_path / "voice.md"
    assert main(["--out", str(out)]) == 0
    text = out.read_text()
    assert text.startswith("# Voice report") and "less hype" in text

    assert main(["--examples"]) == 0
    block = capsys.readouterr().out
    assert "=== RECENT HUMAN EDITS ===" in block and f"CAR-T data {URL}" in block


def test_cli_examples_with_empty_db(db_file, capsys):
    assert main(["--examples"]) == 0
    assert "no examples qualify" in capsys.readouterr().out
    assert main([]) == 0
    assert "# Voice report" in capsys.readouterr().out
