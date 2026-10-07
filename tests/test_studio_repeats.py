"""Step 10's repeat flag: a radar topic or a catalyst that looks like a piece the studio
already has (studio/repeats.py, pure), what it is compared with (studio/store.py:
covered_since), and where the flag shows (the radar page, an automatic piece's brief)."""

from __future__ import annotations

from typing import Any

import pytest

from studio import angles as A
from studio import radar as R
from studio import repeats as RP
from studio import runner
from studio import store as S
from studio.settings import load_studio_config
from tests.test_studio_radar import (  # noqa: F401 (fixtures)
    KW,
    TODAY,
    answer,
    catalyst,
    client,
    started,
    topic,
)
from tests.test_studio_runner import (  # noqa: F401 (fixtures)
    _display_zone,
    _no_dotenv,
    make_piece,
)


@pytest.fixture
def sconn(conn):
    S.ensure_tables(conn)
    return conn


@pytest.fixture
def cfg(monkeypatch) -> dict[str, Any]:
    c = load_studio_config()
    c["radar"].update(enabled=True, repeat_days=30)
    monkeypatch.setattr(runner, "load_studio_config", lambda *a, **k: c)
    return c


def covered(text: str, **kw: Any) -> tuple[RP.Covered, RP.Marks]:
    c = RP.Covered(
        kind="piece",
        id=7,
        label=text.splitlines()[0],
        angle="deal_decoder",
        status="ready",
        text=text,
        **kw,
    )
    return c, RP.covered_marks(c)


def radar_topic(title: str, **kw: Any) -> R.Topic:
    return R.parse_scan(answer(topics=[topic(title, **kw)], catalysts=[]), **KW).topics[0]


# ---- the matcher ------------------------------------------------------------------------


def test_the_same_deal_under_another_angle_is_flagged():
    earlier = covered(
        "Merck pays $700M upfront for SPR2015\nCompanies: Merck (MRK)",
        companies=(("Merck", "MRK"),),
    )
    again = radar_topic(
        "What Merck's SPR2015 upfront says about bispecific pricing", angle="follow_the_money"
    )
    [rep] = RP.find(RP.topic_marks(again), [earlier])
    assert "drug or trial spr2015" in rep.reasons
    assert rep.line().startswith("piece 7 (ready, angle deal_decoder): Merck pays $700M")


@pytest.mark.parametrize(
    "earlier, title, why",
    [
        (
            "Tarlatamab's DeLLphi-304 win",
            "Amgen's Imdelltra launch math for tarlatamab",
            "tarlatamab",
        ),
        (
            "Where KEYNOTE-B15 leaves perioperative IO",
            "KEYNOTE-B15 and the bladder market",
            "keynote-b15",
        ),
        ("AZD0486 in follicular lymphoma", "AstraZeneca's AZD0486 bet", "azd0486"),
        ("NCT05012345 enrols early", "Read-through from NCT05012345", "nct05012345"),
    ],
)
def test_a_shared_drug_code_or_trial_is_enough(earlier, title, why):
    [rep] = RP.find(RP.topic_marks(radar_topic(title, companies=[])), [covered(earlier)])
    assert any(why in r for r in rep.reasons)


def test_a_shared_source_is_enough():
    earlier = covered("Something", sources=("https://www.merck.com/news/a/",))
    [rep] = RP.find(RP.topic_marks(radar_topic("Something else entirely")), [earlier])
    assert "a source" in rep.reasons


def test_a_company_needs_shared_words_too():
    earlier = covered("Merck's subcutaneous Keytruda launch", companies=(("Merck", "MRK"),))
    other = radar_topic("Merck's third quarter earnings", sources=[])
    assert RP.find(RP.topic_marks(other), [earlier]) == []
    same = radar_topic("Merck's subcutaneous Keytruda launch numbers", sources=[])
    [rep] = RP.find(RP.topic_marks(same), [earlier])
    assert rep.reasons == ("company MRK, merck and the words keytruda, launch, subcutaneous",)


@pytest.mark.parametrize(
    "a, b",
    [
        # a meeting's tag is not a drug, an abbreviation in brackets not a ticker
        ("Iovance at SITC2026: objective response rate (ORR)", "Arcellx at SITC2026 (ORR)"),
        # words alone, with no company or drug in common
        ("Bispecific pricing pressure in myeloma", "Bispecific pricing in lymphoma"),
    ],
)
def test_what_is_not_the_same_story_is_not_flagged(a, b):
    assert RP.reasons(RP.marks(a), RP.marks(b)) == ()


def test_a_catalyst_on_a_written_drug_is_flagged():
    earlier = covered("Iovance's lifileucel NSCLC bet")
    cat = R.parse_scan(answer(topics=[], catalysts=[catalyst()]), **KW).catalysts[0]
    [rep] = RP.find(RP.catalyst_marks(cat), [earlier])
    assert any("lifileucel" in r for r in rep.reasons)


# ---- what is compared -------------------------------------------------------------------


def seed_scan(conn, *titles: str, **kw: Any) -> list[int]:
    scan_id = S.start_scan(conn, model="m", effort="max")
    topics = R.parse_scan(answer(topics=[topic(t, **kw) for t in titles], catalysts=[]), **KW)
    return S.add_radar_topics(conn, scan_id, topics.topics)


def test_covered_is_recent_pieces_and_the_queue_with_their_radar_rows(sconn):
    [rid] = seed_scan(sconn, "Merck pays $700M upfront for SPR2015")
    tid = S.queue_topic(sconn, topic="Merck pays $700M upfront", angle="deal_decoder")
    S.set_radar_topic(sconn, rid, status=S.RADAR_QUEUED, topic_id=tid)
    written = make_piece(sconn, topic="Arcellx's anito-cel launch", stage=S.STAGE_READY)
    S.update_piece(sconn, written.id, angle="launch_math")
    gone = make_piece(sconn, topic="Old", stage=S.STAGE_DISCARDED)
    rows = S.covered_since(sconn, "2000-01-01T00:00:00+00:00")
    assert [(c.kind, c.id) for c in rows] == [("queued", tid), ("piece", written.id)]
    queued, piece = rows
    assert queued.companies == (("Merck", "MRK"), ("Spring Bio", ""))
    assert queued.sources == ("https://www.merck.com/news/a",)
    assert piece.angle == "launch_math" and piece.status == S.STAGE_READY
    assert gone.id not in {c.id for c in rows}
    assert S.covered_since(sconn, "2999-01-01T00:00:00+00:00")[1:] == []  # pieces too old


def test_repeat_days_of_zero_switches_the_flag_off(sconn, cfg):
    make_piece(sconn, topic="SPR2015")
    cfg["radar"]["repeat_days"] = 0
    assert runner.covered_for_repeats(sconn, cfg) == []


# ---- where it shows ---------------------------------------------------------------------


def test_the_radar_page_flags_a_topic_that_repeats_a_piece(client, sconn):  # noqa: F811
    piece = make_piece(sconn, topic="Merck pays $700M upfront for SPR2015", stage=S.STAGE_READY)
    S.update_piece(sconn, piece.id, angle="deal_decoder")
    seed_scan(sconn, "Merck's SPR2015 and what bispecifics now cost", angle="follow_the_money")
    seed_scan(sconn, "Arcellx anito-cel launch", companies=[], sources=[])
    body = client.get("/studio/radar").text
    assert body.count("may repeat") == 1
    assert f'<a href="/studio/{piece.id}">piece {piece.id}</a>' in body
    assert "drug or trial spr2015" in body
    assert "Write it anyway" in body and body.count("Write it anyway") == 1


def test_an_automatic_piece_is_told_which_radar_topic_may_repeat(sconn, cfg):
    earlier = make_piece(sconn, topic="Merck pays $700M upfront for SPR2015", stage=S.STAGE_READY)
    [rid, fresh] = seed_scan(
        sconn, "Merck's SPR2015 and what bispecifics now cost", "Arcellx anito-cel launch"
    )
    brief = runner.build_brief(
        sconn,
        cfg,
        make_piece(sconn, topic=""),
        library=A.load_angles(),
        playbook="",
        today=TODAY.isoformat(),
        tzname="UTC",
    )
    assert list(brief.radar_repeats) == [rid]
    [line] = brief.radar_repeats[rid]
    assert line.startswith(f"piece {earlier.id} (ready)")
    from studio import prompt as P

    text = P._topic_block(brief)
    assert f"MAY REPEAT piece {earlier.id}" in text
    assert text.index("MAY REPEAT") < text.index(f"[radar {fresh}]")
