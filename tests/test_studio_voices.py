"""The studio's voices: the playbook's Voices section (studio/voices.py), the draw that
gives each piece one (studio/learn.py:draw_voice), what the editor did with each voice's
pieces, the first-person check (studio/qa.py) and the prompts that carry the voice."""

from __future__ import annotations

import json
import random
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from approval_queue import store as queue_store
from draft.schema import Draft
from studio import evidence as E
from studio import learn as L
from studio import prompt as P
from studio import qa
from studio import store as S
from studio import voices as V
from studio.settings import DEFAULT_PLAYBOOK, load_studio_config

SEED = DEFAULT_PLAYBOOK.read_text(encoding="utf-8")
T0 = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
PLAYBOOK = """# Playbook

## Openings
- Lead with the number.

## Voices
Words for the reader of the playbook.

### desk_note: The desk note
Dry and quick. "I didn't expect that."

### sceptic: The sceptic
Reads the footnotes. "This deal doesn't make any sense to me."

## Still to learn (the feedback loop fills these in)
- Which voice earns replies.
"""


# ---- the Voices section ----------------------------------------------------------------


def test_the_seed_playbook_lists_four_voices_each_speaking_in_the_first_person():
    voices = V.parse(SEED)
    assert [v.key for v in voices] == ["desk_note", "explainer", "sceptic", "storyteller"]
    assert V.problems(SEED) == []
    for v in voices:
        assert v.name and len(v.text.split()) <= V.MAX_WORDS
        assert qa.first_person_sentences(v.text) >= 2, v.key  # a person, with reactions
    # the user's three examples are in the seed, each in a voice of its own
    texts = " ".join(" ".join(v.text.split()) for v in voices)
    for line in (
        "I couldn't believe the data",
        "doesn't make any sense to me",
        "I wonder why they didn't include another dose",
    ):
        assert line in texts


def test_a_voice_is_a_key_a_name_and_how_it_sounds():
    desk, sceptic = V.parse(PLAYBOOK)
    assert desk == V.Voice("desk_note", "The desk note", 'Dry and quick. "I didn\'t expect that."')
    assert sceptic.key == "sceptic" and sceptic.text.startswith("Reads the footnotes.")
    assert desk.brief() == f"The desk note (desk_note)\n{desk.text}"
    assert desk.as_dict() == {"key": "desk_note", "name": "The desk note", "text": desk.text}


def test_a_bare_key_names_itself_and_a_malformed_entry_is_left_out():
    text = (
        "## Voices\n\n### plain_spoken\nSays it straight.\n\n### Not A Key: Bad\nWords.\n\n"
        "### empty: Empty\n\n### plain_spoken: Again\nTwice.\n"
    )
    assert [(v.key, v.name) for v in V.parse(text)] == [("plain_spoken", "Plain spoken")]
    problems = V.problems(text)
    assert any("Not A Key" in p and "lower-case" in p for p in problems)
    assert any("empty" in p and "says nothing" in p for p in problems)
    assert any("listed twice" in p for p in problems)


def test_a_voice_too_long_or_too_many_voices_is_a_problem():
    long = "## Voices\n\n### wordy: Wordy\n" + "word " * (V.MAX_WORDS + 1)
    assert "runs" in V.problems(long)[0]
    many = "## Voices\n" + "".join(
        f"\n### v{i}: Voice {i}\nSays it.\n" for i in range(V.MAX_VOICES + 1)
    )
    assert any("at most" in p for p in V.problems(many))


def test_a_playbook_without_the_section_has_no_problems_and_no_voices():
    assert V.problems("# Playbook\n\n## Openings\n- x\n") == []
    assert V.parse("# Playbook\n") == []
    assert not V.has_section("# Playbook\n\n### desk_note: x\nnot under Voices\n")


def test_a_session_reads_the_playbook_without_the_voices():
    text = V.without(PLAYBOOK)
    assert "## Voices" not in text and "desk_note" not in text and "sceptic" not in text
    assert "## Openings\n- Lead with the number." in text
    assert "## Still to learn" in text and text.endswith("Which voice earns replies.\n")
    assert V.without("# Playbook\n\nNo voices.\n") == "# Playbook\n\nNo voices.\n"
    assert V.without("# Playbook\n\n## Voices\n\n### a: A\nx\n") == "# Playbook\n"
    assert "## Voices" not in V.without(SEED) and "## Still to learn" in V.without(SEED)


def test_a_copy_from_before_voices_gets_the_seeds_before_still_to_learn():
    old = "# Playbook\n\n## Openings\n- mine\n\n## Still to learn\n- more\n"
    text = V.with_seed(old, SEED)
    assert [v.key for v in V.parse(text)] == [v.key for v in V.parse(SEED)]
    assert text.index("## Openings") < text.index("## Voices") < text.index("## Still to learn")
    assert V.without(text).replace("\n\n", "\n") == old.replace("\n\n", "\n")
    # at the end when there is no Still to learn
    assert V.with_seed("# Mine\n", SEED).rstrip().endswith(V.parse(SEED)[-1].text)
    # a section of its own, even an empty one (voices off), is left alone
    off = "# Playbook\n\n## Voices\nNone for now.\n"
    assert V.with_seed(off, SEED) == off and V.parse(off) == []
    assert V.with_seed(PLAYBOOK, SEED) == PLAYBOOK
    assert V.with_seed(old, "# A seed without voices\n") == old


def test_voices_change_by_their_words_not_their_whitespace():
    assert not V.changed(PLAYBOOK, PLAYBOOK.replace("Dry and quick.", "Dry  and\nquick."))
    assert not V.changed(PLAYBOOK, PLAYBOOK.replace("- Lead with the number.", "- Other."))
    assert V.changed(PLAYBOOK, PLAYBOOK.replace("Dry and quick.", "Dry and slow."))
    assert V.changed(PLAYBOOK, PLAYBOOK.replace("### sceptic:", "### doubter:"))
    assert V.changed(PLAYBOOK, V.without(PLAYBOOK))


def test_a_piece_keeps_the_voice_as_it_was_worded_when_it_was_given():
    record = {
        "key": "sceptic",
        "name": "The sceptic",
        "text": "Reads footnotes.",
        "drawn": "random",
    }
    assert V.given("sceptic", record) == V.Voice("sceptic", "The sceptic", "Reads footnotes.")
    assert V.given("desk_note", record) is None  # the record is of another voice
    assert V.given("", record) is None
    assert V.given("sceptic", {"key": "sceptic", "text": " "}) is None
    assert V.given("sceptic", None) is None


# ---- the draw ------------------------------------------------------------------------------


def measured(pid: int, voice: str, relative: float) -> L.Measured:
    m = L.Measured(
        piece_id=pid,
        draft_id=100 + pid,
        posted_at=T0 + timedelta(days=pid),
        value=10.0,
        metrics={},
        voice=voice,
    )
    m.baseline, m.relative = 10.0, relative
    return m


def draw(pieces, voices, previous="", seed=0, min_measured=30) -> L.VoiceDraw | None:
    return L.draw_voice(
        pieces,
        voices,
        previous=previous,
        rng=random.Random(seed),
        min_measured=min_measured,
        prior_sd=0.5,
        default_sd=0.8,
    )


def test_the_voice_is_drawn_at_random_and_never_the_one_before():
    keys = ["desk_note", "explainer", "sceptic", "storyteller"]
    got = Counter()
    for seed in range(2000):
        d = draw([], keys, previous="sceptic", seed=seed)
        assert d is not None and d.how == "random" and d.measured == 0
        got[d.key] += 1
    assert "sceptic" not in got
    assert set(got) == {"desk_note", "explainer", "storyteller"}
    assert min(got.values()) > 550  # about a third each


def test_one_voice_is_always_given_and_none_gives_none():
    assert draw([], ["only"], previous="only") == L.VoiceDraw("only", "only", 0)
    assert draw([], [], previous="x") is None
    assert draw([], ["", ""]) is None
    # the voice before is not one of today's: every voice is on offer
    assert draw([], ["a", "b"], previous="retired").key in ("a", "b")


def test_after_enough_scored_pieces_the_draw_follows_what_x_says():
    pieces = [measured(i, "good", 3.0) for i in range(15)]
    pieces += [measured(100 + i, "poor", 0.3) for i in range(15)]
    got = Counter(draw(pieces, ["good", "poor"], seed=s).key for s in range(300))
    first = draw(pieces, ["good", "poor"])
    assert first.how == "evidence" and first.measured == 30
    assert got["good"] > 280
    # one short of the floor: still even
    assert draw(pieces[:29], ["good", "poor"]).how == "random"
    # a floor of 0 never follows the evidence
    assert draw(pieces, ["good", "poor"], min_measured=0).how == "random"
    # pieces without a voice (from before voices) do not count towards the floor
    unvoiced = [measured(200 + i, "", 1.0) for i in range(40)]
    assert draw(unvoiced, ["good", "poor"]).how == "random"


def test_voice_is_an_arm_the_rewrite_sees_and_a_session_does_not():
    pieces = [measured(1, "sceptic", 2.0), measured(2, "explainer", 0.5)]
    stats = L.arm_stats(pieces, "voice")
    assert [s.value for s in stats] == ["sceptic", "explainer"]
    full = L.evidence_block(pieces)
    assert "- Voices: sceptic 2.0x (1 piece); explainer 0.5x (1 piece)." in full
    assert "sceptic voice" in full
    session = L.evidence_block(pieces, voices=False)
    assert "Voices" not in session and "sceptic" not in session and "explainer" not in session


# ---- what the editor did with each voice ----------------------------------------------------


def test_the_share_of_words_changed_counts_cuts_and_additions_alike():
    assert L.edit_share("one two three four", "one two three four") == 0
    assert L.edit_share("", "") == 0
    assert L.edit_share("one two", "three four") == 1
    assert L.edit_share("one two three four", "one two three") == pytest.approx(1 - 6 / 7)
    assert L.edit_share("a b c", "a b c d") == pytest.approx(1 - 6 / 7)


def test_per_voice_the_one_rewritten_least_comes_first():
    reviews = [
        L.Reviewed(1, "sceptic", L.REVIEW_POSTED, 0.0, 0),
        L.Reviewed(2, "sceptic", L.REVIEW_APPROVED, 0.1, 1),
        L.Reviewed(3, "explainer", L.REVIEW_POSTED, 0.4, 0),
        L.Reviewed(4, "explainer", L.REVIEW_REJECTED, 0.0, 2),
        L.Reviewed(5, "storyteller", L.REVIEW_REJECTED, 0.0, 0),
        L.Reviewed(6, "", L.REVIEW_POSTED, 0.9, 0),  # no voice: not counted
    ]
    sceptic, explainer, storyteller = L.voice_edits(reviews)
    assert sceptic == L.VoiceEdits("sceptic", 2, 1, pytest.approx(0.05), 1, 0)
    assert (explainer.voice, explainer.decided, explainer.kept, explainer.rejected) == (
        "explainer",
        2,
        1,
        1,
    )
    assert explainer.changed == pytest.approx(0.4) and explainer.revisions == 2
    assert storyteller.changed is None and storyteller.kept == 0
    line = L.voice_edits_line(sceptic)
    assert line == (
        "- sceptic: 2 pieces decided; 1 of 2 kept changed by hand, 5% of the words on "
        "average; 1 revision asked; 0 rejected"
    )
    assert "kept" not in L.voice_edits_line(storyteller)


def test_the_rewrite_is_told_each_pieces_voice_and_what_the_editor_did_with_each():
    pieces = [measured(1, "sceptic", 2.0)]
    by_voice = L.voice_edits([L.Reviewed(1, "sceptic", L.REVIEW_POSTED, 0.0, 0)])
    system, user = L.rewrite_prompt(
        PLAYBOOK, pieces, [], evidence="E", max_words=900, by_voice=by_voice
    )
    assert "## Voices" in system and "keep a voice's key" in system
    assert "I couldn't believe the data" in system  # never written out of the playbook
    assert "voice sceptic;" in user
    assert "WHAT THE EDITOR DID WITH EACH VOICE'S PIECES" in user
    assert "- sceptic: 1 piece decided" in user
    _, empty = L.rewrite_prompt(PLAYBOOK, [], [], evidence="", max_words=900)
    assert "(none decided yet)" in empty


def rewrite_reply(playbook: str) -> str:
    return json.dumps({"playbook": playbook, "changelog": ["x"]})


def with_cards(text: str) -> str:
    return text.replace(
        "## Voices",
        "## Substance\n- x\n\n## Mistakes caught\n- x\n\n"
        "## Format\n- x\n\n## Cards\n- x\n\n## Voices",
    )


def test_a_rewrite_needs_a_readable_voices_section_with_two_to_six_voices():
    good = with_cards(PLAYBOOK)
    assert L.parse_rewrite(rewrite_reply(good), max_words=900).playbook == good
    with pytest.raises(L.RewriteRejected, match="lacks ## Voices"):
        L.parse_rewrite(rewrite_reply(V.without(good)), max_words=900)
    one = good.replace("### sceptic: The sceptic\n", "").replace(
        'Reads the footnotes. "This deal doesn\'t make any sense to me."\n', ""
    )
    with pytest.raises(L.RewriteRejected, match="lists 1 voice"):
        L.parse_rewrite(rewrite_reply(one), max_words=900)
    broken = good.replace("### sceptic: The sceptic", "### The Sceptic")
    with pytest.raises(L.RewriteRejected, match="its voices"):
        L.parse_rewrite(rewrite_reply(broken), max_words=900)


def test_the_voices_are_left_out_of_the_playbooks_word_count():
    good = with_cards(PLAYBOOK)
    words = len(V.without(good).split())
    long_voice = good.replace("Dry and quick.", "Dry and quick. " + "word " * 100)
    assert L.parse_rewrite(rewrite_reply(long_voice), max_words=words).playbook
    with pytest.raises(L.RewriteRejected, match="runs"):
        L.parse_rewrite(rewrite_reply(good + "word " * 200), max_words=words)


# ---- the first-person check -------------------------------------------------------------------


@pytest.mark.parametrize(
    ("sentence", "speaks"),
    [
        ("I couldn't believe the data.", True),
        ("This deal doesn't make any sense to me.", True),
        ("I wonder why they didn't include another dose.", True),
        ("My read: the market is pricing the wrong risk.", True),
        ("I'd want the 12-month data before I believed it.", True),
        ("I'm not sure yet.", True),
        ("The phase I trial enrolled 40 patients.", False),
        ("Type I interferon signalling is the point.", False),
        ("MHC class I molecules are lost.", False),
        ("Grade I and II events were common.", False),
        ("Phase I/II data arrive in Q3.", False),
        ("A me-too drug in a crowded class.", False),
        ("Multiple myeloma remains the market.", False),
        ("ME/CFS is not cancer.", False),
        ("Merck paid $400 million.", False),
    ],
)
def test_a_sentence_speaks_as_the_writer_only_in_the_first_person(sentence, speaks):
    assert qa.speaks_as_writer(sentence) is speaks


def test_the_floor_grows_with_the_length_up_to_a_cap():
    assert [qa.first_person_wanted(n, 1200, 6) for n in (0, 500, 1200, 1201, 6000, 30000)] == [
        0,
        1,
        1,
        2,
        5,
        6,
    ]
    assert qa.first_person_wanted(30000, 1200, 0) == 25  # no cap
    assert qa.first_person_wanted(5000, 0, 6) == 0  # no check
    text = "I couldn't believe it. The trial ran. My read: odd.\n• I wonder why.\nThe end."
    assert qa.first_person_sentences(text) == 3


def piece_files(*posts: str) -> qa.PieceFiles:
    return qa.PieceFiles(title="t", angle="a", shape="long_post", posts=list(posts))


VOICES_CFG = {"first_person_every_chars": 1200, "first_person_max": 6}


def test_a_post_that_reads_like_a_report_goes_back_to_the_session():
    report = qa.Report()
    qa.check_first_person(
        piece_files("Merck paid $400 million. The trial ran."), report, VOICES_CFG
    )
    [problem] = report.fixable
    assert "reads like a report, not a person: 0 sentences speak as you" in problem
    assert "needs at least 1" in problem and "I couldn't believe the data" in problem
    assert not report.blocking


def test_a_post_with_enough_reactions_or_no_floor_passes():
    ok = qa.Report()
    qa.check_first_person(
        piece_files("Merck paid $400 million. I didn't expect that."), ok, VOICES_CFG
    )
    assert ok.fixable == []
    long = " ".join(["The trial ran."] * 300) + " I didn't expect that."
    short = qa.Report()
    qa.check_first_person(piece_files(long), short, VOICES_CFG)
    assert "1 sentence speaks as you" in short.fixable[0] and "at least 4" in short.fixable[0]
    for cfg in (None, {}, {"first_person_every_chars": 0}):
        quiet = qa.Report()
        qa.check_first_person(piece_files(long), quiet, cfg)
        assert quiet.fixable == []


def test_the_shipped_settings_ask_for_a_person():
    cfg = load_studio_config()["voices"]
    assert cfg["enabled"] is True and cfg["lean_min_measured"] == 30
    assert cfg["first_person_every_chars"] == 1200 and cfg["first_person_max"] == 6


def test_the_voice_guide_asks_every_piece_to_sound_like_a_person():
    from studio.settings import read_brief

    guide = read_brief("voice.md")
    assert "## Sound like a person" in guide
    for line in (
        "I couldn't believe the data",
        "This deal doesn't make any sense to me",
        "I wonder why they didn't include another dose",
        "invented experiences, no",
    ):
        assert line in guide


# ---- the prompts ---------------------------------------------------------------------------


def brief(voice: V.Voice | None) -> P.Brief:
    return P.Brief(
        piece_id=7,
        today="2026-10-05",
        timezone="America/Los_Angeles",
        workspace="/w",
        reference_dir="/w/reference",
        references=[],
        voice=voice,
    )


SCEPTIC = V.Voice("sceptic", "The sceptic", "Reads the footnotes first.")


def test_the_write_stage_is_told_its_voice_and_research_what_it_will_need():
    write = P.write_prompt(brief(SCEPTIC), note="lead with the deal")
    assert "YOUR VOICE FOR THIS PIECE" in write
    assert "The sceptic (sceptic)\nReads the footnotes first." in write
    assert write.index("lead with the deal") < write.index("YOUR VOICE") < write.index("CHOOSE")
    assert "the editor wins" in write
    research = P.research_prompt(brief(SCEPTIC))
    assert "THE VOICE THIS PIECE WILL BE WRITTEN IN" in research and "The sceptic" in research
    for text in (P.write_prompt(brief(None)), P.research_prompt(brief(None))):
        assert "VOICE" not in text


def test_a_revision_is_reminded_of_the_pieces_voice():
    text = P.revise_prompt("make it shorter", voice=SCEPTIC)
    assert "THE PIECE'S VOICE (keep it, unless the editor asks for another)" in text
    assert "Reads the footnotes first." in text
    assert "VOICE" not in P.revise_prompt("make it shorter")


# ---- the database --------------------------------------------------------------------------------


@pytest.fixture
def sconn(conn):
    S.ensure_tables(conn)
    return conn


def make_piece(conn, voice: str = "", stage: str = S.STAGE_READY) -> S.Piece:
    pid = S.create_piece(
        conn,
        origin=S.ORIGIN_AUTO,
        topic="t",
        cluster_id=None,
        requested_angle="",
        checkpoint=False,
        session_id="s",
        workspace="/nowhere",
        model="m",
        effort="max",
    )
    S.update_piece(conn, pid, voice=voice, stage=stage)
    piece = S.get_piece(conn, pid)
    assert piece is not None
    return piece


def test_an_older_database_gains_the_voice_column(tmp_path):
    import sqlite3

    path = tmp_path / "old.sqlite"
    old = sqlite3.connect(path)
    old.executescript(
        """CREATE TABLE studio_pieces (
            id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL, origin TEXT NOT NULL, topic TEXT NOT NULL DEFAULT '',
            cluster_id INTEGER, requested_angle TEXT NOT NULL DEFAULT '',
            angle TEXT NOT NULL DEFAULT '', shape TEXT NOT NULL DEFAULT '',
            hook_style TEXT NOT NULL DEFAULT '', title TEXT NOT NULL DEFAULT '',
            stage TEXT NOT NULL, checkpoint INTEGER NOT NULL DEFAULT 0,
            session_id TEXT NOT NULL, workspace TEXT NOT NULL, model TEXT NOT NULL,
            effort TEXT NOT NULL DEFAULT '', draft_id INTEGER,
            request TEXT NOT NULL DEFAULT '', request_note TEXT NOT NULL DEFAULT '',
            error TEXT NOT NULL DEFAULT '', meta_json TEXT NOT NULL DEFAULT '{}');
        INSERT INTO studio_pieces (created_at, updated_at, origin, stage, session_id,
            workspace, model)
            VALUES ('2026-01-01', '2026-01-01', 'auto', 'ready', 's', '/w', 'm');"""
    )
    old.commit()
    old.close()
    conn = S.connect(path)
    [piece] = S.list_pieces(conn)
    assert piece.voice == ""
    S.update_piece(conn, piece.id, voice="sceptic")
    assert S.get_piece(conn, piece.id).voice == "sceptic"
    conn.close()


def test_the_voice_a_new_piece_is_not_given_is_the_newest_pieces(sconn):
    assert S.last_voice(sconn) == ""
    a = make_piece(sconn, "sceptic")
    b = make_piece(sconn, "explainer", stage=S.STAGE_DISCARDED)  # discarded: not counted
    make_piece(sconn, "")  # no voice: not counted
    assert S.last_voice(sconn) == "sceptic"
    c = make_piece(sconn, "storyteller")
    assert S.last_voice(sconn) == "storyteller"
    assert S.last_voice(sconn, exclude=c.id) == "sceptic"
    assert {a.voice, b.voice} == {"sceptic", "explainer"}


def queue(conn, piece: S.Piece, text: str = "I didn't expect that. The trial ran.") -> int:
    draft_id = queue_store.insert_draft(
        conn,
        item_id=queue_store.studio_item_id(piece.id),
        model="m (studio)",
        draft=Draft(thread=[text], suggested_visual="", why_it_matters="w"),
    )
    S.update_piece(conn, piece.id, draft_id=draft_id)
    return draft_id


def test_what_the_editor_did_with_each_voiced_piece_is_read_from_the_queue(sconn):
    edited = make_piece(sconn, "sceptic")
    d1 = queue(sconn, edited, "one two three four five six seven eight nine ten")
    queue_store.edit(
        sconn,
        d1,
        thread=["one two three four five six seven eight nine TEN"],
        approve_after=False,
    )
    queue_store.drop_image(sconn, d1)  # logs the same text twice: not an edit
    queue_store.approve(sconn, d1)
    revised = make_piece(sconn, "sceptic")
    d2 = queue(sconn, revised)
    queue_store.revise(
        sconn,
        d2,
        draft=Draft(thread=["I wondered. Revised."], suggested_visual="", why_it_matters="w"),
        model="m (studio)",
        note="shorter",
    )
    queue_store.reject(sconn, d2)
    pending = make_piece(sconn, "explainer")
    queue(sconn, pending)  # not decided yet: not counted
    unvoiced = make_piece(sconn, "")
    queue_store.approve(sconn, queue(sconn, unvoiced))  # no voice: not counted
    got = {r.piece_id: r for r in E.reviews(sconn)}
    assert set(got) == {edited.id, revised.id}
    assert got[edited.id].outcome == L.REVIEW_APPROVED
    assert got[edited.id].changed == pytest.approx(0.1)
    assert got[edited.id].revisions == 0
    assert got[revised.id].outcome == L.REVIEW_REJECTED and got[revised.id].revisions == 1
    [row] = [r for r in S.fetch_studio_reviews(sconn) if r.piece_id == edited.id]
    assert row.status == queue_store.STATUS_APPROVED and not row.posted and len(row.edits) == 1


def test_the_reviews_are_empty_without_the_queues_tables(tmp_path):
    conn = S.connect(tmp_path / "studio_only.sqlite")
    assert S.fetch_studio_reviews(conn) == []
    assert E.reviews(conn) == []
    conn.close()


def test_a_draft_on_x_counts_as_posted(sconn):
    from publish import store as publish_store

    piece = make_piece(sconn, "explainer")
    draft_id = queue(sconn, piece)
    queue_store.approve(sconn, draft_id)
    publish_store.connect(queue_store.db_path()).close()  # step 3's tables
    publish_store.record_post(
        sconn,
        draft_id=draft_id,
        text="x",
        kind="thread",
        position=1,
        slot="manual",
        tweet_id="123",
        posted_at=T0,
    )
    [review] = E.reviews(sconn)
    assert review.outcome == L.REVIEW_POSTED


def test_the_seed_is_what_an_old_playbook_copy_reads_its_voices_from(tmp_path):
    from studio import playbook as PB

    (tmp_path / "studio_playbook.md").write_text(
        "# Playbook\n\n## Openings\n- mine\n\n## Still to learn\n- x\n", encoding="utf-8"
    )
    text = PB.current_text(tmp_path)
    assert "- mine" in text and [v.key for v in V.parse(text)] == [v.key for v in V.parse(SEED)]
    assert PB.current_text(tmp_path / "nowhere") == SEED  # no copy: the seed itself


def test_voices_section_is_in_the_seed_where_a_rewrite_must_keep_it():
    assert SEED.index("## Format") < SEED.index(V.HEADING) < SEED.index("## Still to learn")
    assert Path(DEFAULT_PLAYBOOK).name == "playbook.md"


def test_with_the_voices_turned_off_a_rewrite_may_keep_them_off():
    off = (
        with_cards(PLAYBOOK).split("Words for the reader")[0]
        + "None for now.\n\n"
        + ("## Still to learn (the feedback loop fills these in)\n- x\n")
    )
    assert V.has_section(off) and V.parse(off) == []
    with pytest.raises(L.RewriteRejected, match="lists 0 voice"):
        L.parse_rewrite(rewrite_reply(off), max_words=900)
    assert L.parse_rewrite(rewrite_reply(off), max_words=900, voices_off=True).playbook == off
    _, user = L.rewrite_prompt(off, [], [], evidence="", max_words=900)
    assert "The editor has turned the voices off" in user
    _, user = L.rewrite_prompt(PLAYBOOK, [], [], evidence="", max_words=900)
    assert "turned the voices off" not in user
