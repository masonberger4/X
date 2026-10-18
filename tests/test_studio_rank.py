"""What to write next: a story's traits (studio/traits.py), the estimate and the newness
rule (studio/rank.py), the crowd lookups (studio/crowd.py), and where they show (the
scan's answer, the learning loop's arms, an automatic piece's brief, the radar page)."""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from studio import angles as A
from studio import crowd as CR
from studio import evidence as E
from studio import learn as L
from studio import radar as R
from studio import rank as RK
from studio import runner
from studio import store as S
from studio import traits as TR
from studio.settings import load_studio_config
from tests.test_studio_radar import (  # noqa: F401 (fixtures)
    KW,
    TODAY,
    answer,
    client,
    started,
    topic,
)
from tests.test_studio_runner import (  # noqa: F401 (fixtures)
    _display_zone,
    _no_dotenv,
    make_piece,
)

T0 = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)


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


def measured(pid: int, rel: float, **traits: str) -> L.Measured:
    m = L.Measured(
        piece_id=pid,
        draft_id=100 + pid,
        posted_at=T0 + timedelta(days=pid),
        value=rel,
        metrics={},
        traits=dict(traits),
    )
    m.relative = rel
    return m


# --- traits -------------------------------------------------------------------------------


def test_read_traits_keeps_only_known_values():
    got = TR.read_traits(
        {"cap": "Small", "stage": "commercial", "beat": "io core", "debate": "maybe", "x": 1}
    )
    assert got == {"cap": "small", "stage": "commercial", "beat": "io_core"}
    assert TR.read_traits(None) == {} and TR.read_traits("small") == {}


def test_crowd_bucket_and_tickers():
    assert TR.crowd_bucket(21029, large=20000, medium=5000) == "large"
    assert TR.crowd_bucket(6000, large=20000, medium=5000) == "medium"
    assert TR.crowd_bucket(10, large=20000, medium=5000) == "small"
    assert TR.crowd_bucket(None, large=20000, medium=5000) == ""
    assert TR.crowd_ticker("$iova") == "IOVA"
    assert TR.crowd_ticker("9926.HK") == "" and TR.crowd_ticker("") == ""


def test_company_keys_meet_across_spellings():
    a = TR.company_keys([("Iovance Biotherapeutics", "IOVA")])
    b = TR.company_keys([("Iovance", "")])
    assert a & b
    assert not TR.company_keys([("Merck", "MRK")]) & a


# --- the estimate ---------------------------------------------------------------------------


def test_the_estimate_learns_traits_not_companies():
    pool = [
        measured(1, 6.0, crowd="large", cap="small"),
        measured(2, 5.0, crowd="large", cap="small"),
        measured(3, 0.5, crowd="small", cap="micro"),
        measured(4, 0.4, crowd="small", cap="micro"),
    ]
    kw = dict(repeat_penalty=0.6, prior_sd=0.5, post_sd=0.8)
    crowd = RK.estimate({"crowd": "large", "cap": "small"}, pool, repeats=0, **kw)
    quiet = RK.estimate({"crowd": "small", "cap": "micro"}, pool, repeats=0, **kw)
    assert crowd.times > 1 > quiet.times
    # a covered company with the same traits is ranked lower, never higher
    again = RK.estimate({"crowd": "large", "cap": "small"}, pool, repeats=2, **kw)
    assert math.isclose(again.times, crowd.times * 0.36)
    # nothing measured shares a trait: no estimate, only the penalty applies
    unknown = RK.estimate({"stage": "preclinical"}, pool, repeats=0, **kw)
    assert unknown.times is None and unknown.measured == 4


def test_order_puts_the_explore_pick_first_then_the_estimates():
    e = {
        1: RK.Estimate({}, 0.8, 0, 3),
        2: RK.Estimate({"new_company": "yes"}, None, 0, 3, explore=True),
        3: RK.Estimate({}, 2.0, 0, 3),
        4: RK.Estimate({}, None, 0, 3),
    }
    assert RK.order(e) == [2, 3, 1, 4]
    assert RK.explore_pick([5, 6, 7], seed="2026-10-17") == RK.explore_pick(
        [7, 6, 5], seed="2026-10-17"
    )
    assert RK.explore_pick([], seed="x") is None


def test_newness_rule():
    assert RK.Newness([False, False], every=3, need=2).due
    assert RK.Newness([True, False], every=3, need=2).due
    assert not RK.Newness([True, True, False], every=3, need=2).due
    assert not RK.Newness([False, False], every=0, need=2).due
    n = RK.Newness([True, False, True], every=3, need=2)
    assert n.share() == (2, 3) and "2 of the account's last 3" in n.line()
    assert "should be on a company" in RK.Newness([False], every=3, need=2).line()


# --- the learning loop ----------------------------------------------------------------------


def test_trait_arms_are_learned_and_shown_to_sessions():
    pool = [measured(1, 4.0, crowd="large"), measured(2, 0.5, crowd="small")]
    stats = L.arm_stats(pool, "crowd")
    assert [s.value for s in stats] == ["large", "small"]
    pool[0].opening = pool[1].opening = "x"
    text = L.evidence_block(pool)
    assert "Retail crowd" in text and "large 4.0x" in text


def research_piece(conn, companies, *, traits=None, stage=S.STAGE_READY, **kw):
    info: dict[str, Any] = {"companies": [{"name": n, "ticker": t} for n, t in companies]}
    if traits:
        info["traits"] = traits
    return make_piece(conn, stage=stage, meta={"research": info}, **kw)


def test_piece_traits_join_research_crowd_and_newness(sconn, cfg):
    first = research_piece(sconn, [("Iovance", "IOVA")], traits={"cap": "small"})
    again = research_piece(sconn, [("Iovance Biotherapeutics", "IOVA")])
    other = research_piece(sconn, [("Merck", "MRK")])
    S.set_crowd(sconn, "IOVA", 21029)
    got = E.piece_traits(sconn, cfg, S.list_pieces(sconn, 10))
    assert got[first.id] == {"cap": "small", "crowd": "large", "new_company": "yes"}
    assert got[again.id]["new_company"] == "no"
    assert got[other.id] == {"new_company": "yes"}
    n = E.newness(sconn, cfg)
    assert n.recent == [True, False, True]


# --- the crowd ------------------------------------------------------------------------------


def test_crowd_refresh_looks_up_what_is_due_and_never_raises(sconn, cfg):
    calls = []

    def fetch(ticker, rcfg):
        calls.append(ticker)
        if ticker == "BAD":
            raise CR.CrowdError("refused")
        return {"IOVA": 21029, "MRK": 9000}[ticker]

    rcfg = cfg["ranking"]
    got = CR.refresh(
        sconn, rcfg, ["$IOVA", "MRK", "IOVA", "9926.HK", "BAD"], fetch=fetch, sleep=lambda s: None
    )
    assert got == (2, 1) and calls == ["IOVA", "MRK", "BAD"]
    rows = S.crowd(sconn)
    assert rows["IOVA"].watchers == 21029 and rows["BAD"].error == "refused"
    # within crowd_refresh_hours nothing is looked up again; a failure keeps the last count
    calls.clear()
    assert CR.refresh(sconn, rcfg, ["IOVA"], fetch=fetch) == (0, 0) and not calls
    S.set_crowd(sconn, "IOVA", None, error="later failure")
    assert S.crowd(sconn)["IOVA"].watchers == 21029


def test_fetch_watchers_reads_the_symbol_page(monkeypatch, cfg):
    from ingest import http

    monkeypatch.undo()  # the conftest refusal; the page itself is faked below
    seen = {}

    def get_json(url, **kw):
        seen["url"], seen["ua"] = url, kw.get("user_agent")
        return {"symbol": {"symbol": "IOVA", "watchlist_count": 21029}}

    monkeypatch.setattr(http, "get_json", get_json)
    assert CR.fetch_watchers("IOVA", cfg["ranking"]) == 21029
    assert seen["url"].endswith("/IOVA.json") and seen["ua"] == "python-httpx/0.27"
    monkeypatch.setattr(http, "get_json", lambda url, **kw: {"symbol": {}})
    with pytest.raises(CR.CrowdError):
        CR.fetch_watchers("IOVA", cfg["ranking"])


# --- the scan, the brief and the page ---------------------------------------------------------


def test_the_scan_reads_traits_and_the_feed_lead():
    raw = answer(
        topics=[
            {
                **topic("Iovance after the raise"),
                "traits": {"cap": "small", "debate": "yes"},
                "feed_lead": "F2",
            },
            {**topic("Merck's SC Keytruda"), "feed_lead": "nope"},
        ],
        catalysts=[],
    )
    got = R.parse_scan(raw, **KW)
    assert got.topics[0].traits == {"cap": "small", "debate": "yes"}
    assert got.topics[0].feed_lead == 2 and got.topics[1].feed_lead is None


def test_radar_rows_keep_traits_and_the_feed_story(sconn):
    scan_id = S.start_scan(sconn, model="m", effort="max")
    got = R.parse_scan(
        answer(topics=[{**topic("A"), "traits": {"cap": "mid"}}, topic("B")], catalysts=[]), **KW
    )
    [a, b] = S.add_radar_topics(sconn, scan_id, got.topics, [41, None])
    row = S.get_radar_topic(sconn, a)
    assert row.traits == {"cap": "mid"} and row.feed_cluster == 41
    assert row.to_topic().traits == {"cap": "mid"}
    assert S.get_radar_topic(sconn, b).feed_cluster is None


def test_the_brief_ranks_the_radar_and_says_when_a_new_company_is_due(sconn, cfg):
    for _ in range(2):
        research_piece(sconn, [("Iovance", "IOVA")])
    scan_id = S.start_scan(sconn, model="m", effort="max")
    got = R.parse_scan(
        answer(
            topics=[
                {**topic("Iovance again"), "companies": [{"name": "Iovance", "ticker": "IOVA"}]},
                {**topic("Kyverna in MS"), "companies": [{"name": "Kyverna", "ticker": "KYTX"}]},
            ],
            catalysts=[],
        ),
        **KW,
    )
    [old, new] = S.add_radar_topics(sconn, scan_id, got.topics)
    brief = runner.build_brief(
        sconn,
        cfg,
        make_piece(sconn, topic=""),
        library=A.load_angles(),
        playbook="",
        today=TODAY.isoformat(),
        tzname="UTC",
    )
    assert [rid for rid, _ in brief.radar] == [new, old]
    assert "a company new to the account" in brief.radar_notes[new]
    assert "2 recent piece(s) on its company" in brief.radar_notes[old]
    assert "should be on a company" in brief.newness
    from studio import prompt as P

    text = P._topic_block(brief)
    assert "NEW COMPANIES" in text and "The app's ranking" in text


def test_the_radar_page_shows_estimates_and_the_newness_rule(client, sconn):  # noqa: F811
    research_piece(sconn, [("Iovance", "IOVA")])
    scan_id = S.start_scan(sconn, model="m", effort="max")
    got = R.parse_scan(
        answer(
            topics=[
                {
                    **topic("Kyverna in MS"),
                    "companies": [{"name": "Kyverna", "ticker": "KYTX"}],
                    "traits": {"cap": "small"},
                }
            ],
            catalysts=[],
        ),
        **KW,
    )
    S.add_radar_topics(sconn, scan_id, got.topics)
    body = client.get("/studio/radar").text
    assert "new company" in body and "try something new" in body
    assert "company size: small" in body
    assert "should be on a company" in body


def test_radar_sections_group_the_topics_in_order():
    from types import SimpleNamespace as NS

    from studio import web

    t = [NS(id=i, status=S.RADAR_NEW) for i in range(1, 6)]
    t[4].status = S.RADAR_QUEUED
    new_co = RK.Estimate({"new_company": "yes"}, 1.0, 0, 3)
    covered = RK.Estimate({"new_company": "no"}, 2.0, 1, 3)
    got = web.radar_sections(t, {1: ["flag"], 2: []}, {1: new_co, 2: new_co, 3: covered, 5: new_co})
    assert {k: [x.id for x in v] for k, v in got.items()} == {
        "repeat": [1],
        "new_company": [2],
        "fresh": [3, 4],
        "taken": [5],
    }


def test_the_radar_page_shows_its_groups_in_the_editors_order(client, sconn):  # noqa: F811
    research_piece(sconn, [("Iovance", "IOVA")])
    scan_id = S.start_scan(sconn, model="m", effort="max")
    got = R.parse_scan(
        answer(
            topics=[
                {**topic("Kyverna in MS"), "companies": [{"name": "Kyverna", "ticker": "KYTX"}]},
                {**topic("Iovance again"), "companies": [{"name": "Iovance", "ticker": "IOVA"}]},
            ],
            catalysts=[],
        ),
        **KW,
    )
    S.add_radar_topics(sconn, scan_id, got.topics)
    body = client.get("/studio/radar").text
    heads = ["New company", "Completely new", "Catalysts", "Possible repeats", "Queued or written"]
    places = [body.index(f'<strong class="section-title">{h}</strong>') for h in heads]
    assert places == sorted(places)
    new_group = body[places[0] : places[1]]
    assert "Kyverna in MS" in new_group and "Iovance again" not in new_group
    assert "Iovance again" in body[places[1] : places[2]]
    assert body.count('<details class="radar-section"') == 6
