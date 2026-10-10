"""Market movers: the screen (pure), the story check's answer, and one run end to end with
the prices and the Claude call faked. The price fixtures are synthetic: Yahoo Finance's
chart endpoint could not be reached from the build sandbox, so they follow its documented
shape ({"chart": {"result": [{"meta", "timestamp", "indicators"}]}})."""

from __future__ import annotations

import json
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from movers import check as CK
from movers import runner
from movers import screen as SC
from movers import store as M
from movers.settings import load_movers_config

NY = ZoneInfo("America/New_York")


def ny(y, mo, d, h=9, mi=30):
    return datetime(y, mo, d, h, mi, tzinfo=NY)


def chart(symbol, bars, name=""):
    return {
        "chart": {
            "result": [
                {
                    "meta": {
                        "symbol": symbol,
                        "exchangeTimezoneName": "America/New_York",
                        "longName": name,
                    },
                    "timestamp": [int(at.timestamp()) for at, _, _ in bars],
                    "indicators": {
                        "quote": [
                            {"close": [c for _, c, _ in bars], "volume": [v for *_, v in bars]}
                        ]
                    },
                }
            ],
            "error": None,
        }
    }


CFG = {
    "threshold_pct": 5.0,
    "min_price": 1.0,
    "min_dollar_volume": 2_000_000,
    "min_extended_volume": 20_000,
}

# Wed Oct 7 and Thu Oct 8 2026 sessions; the screen runs Fri Oct 9 at 09:00 New York.
DAILY = [(ny(2026, 10, 7), 100.0, 1_000_000), (ny(2026, 10, 8), 110.0, 2_000_000)]
INTRADAY = [
    (ny(2026, 10, 8, 15, 45), 110.5, 50_000),
    (ny(2026, 10, 8, 17, 0), 115.0, 15_000),
    (ny(2026, 10, 8, 19, 0), 121.0, 15_000),
    (ny(2026, 10, 9, 7, 0), 100.0, 10_000),
    (ny(2026, 10, 9, 8, 45), 99.0, 30_000),
]
NOW = ny(2026, 10, 9, 9, 0)


def measured(daily=DAILY, intraday=INTRADAY, now=NOW):
    return SC.measure(
        "CRSP",
        SC.parse_chart(chart("CRSP", daily)),
        SC.parse_chart(chart("CRSP", intraday)) if intraday is not None else None,
        now=now,
        company="CRISPR Therapeutics",
    )


def test_measure_finds_the_session_after_hours_and_premarket_moves():
    m = measured()
    by = {x.session: x for x in m.moves}
    assert m.session_day.isoformat() == "2026-10-08"
    assert by["regular"].pct == pytest.approx(10.0)
    assert by["after_hours"].pct == pytest.approx(10.0)  # 121 against the 110 close
    assert by["after_hours"].volume == 30_000
    assert by["premarket"].day.isoformat() == "2026-10-09"
    assert by["premarket"].pct == pytest.approx(-10.0)  # 99 against the 110 close


def test_a_live_daily_bar_for_today_is_not_a_completed_session():
    daily = [*DAILY, (ny(2026, 10, 9), 140.0, 10)]
    assert measured(daily=daily).session_day.isoformat() == "2026-10-08"
    after_close = measured(daily=daily, intraday=None, now=ny(2026, 10, 9, 16, 30))
    assert after_close.session_day.isoformat() == "2026-10-09"


def test_screen_keeps_moves_over_the_threshold_with_enough_volume():
    m = SC.screen(measured(), CFG)
    assert {x.session for x in m.flagged} == {"regular", "after_hours", "premarket"}
    thin = SC.screen(measured(), {**CFG, "min_extended_volume": 45_000})
    assert {x.session for x in thin.flagged} == {"regular"}
    assert SC.screen(measured(), {**CFG, "threshold_pct": 15}) is None
    assert SC.screen(measured(), {**CFG, "min_dollar_volume": 10**9}) is None
    assert SC.screen(measured(), {**CFG, "min_price": 500}) is None


def test_parse_chart_drops_empty_bars_and_refuses_an_error():
    raw = chart("X", DAILY)
    raw["chart"]["result"][0]["indicators"]["quote"][0]["close"][0] = None
    assert len(SC.parse_chart(raw).bars) == 1
    with pytest.raises(SC.ChartError):
        SC.parse_chart({"chart": {"result": None, "error": {"description": "No data found"}}})
    with pytest.raises(SC.ChartError):
        SC.parse_chart({"nope": 1})


def test_universe_takes_the_configured_companies_extras_and_exclusions():
    root = {
        "companies": {"feeds": [{"name": "Legend", "ticker": "LEGN"}, {"name": "NoTicker"}]},
        "branding": {"companies": [{"name": "Pfizer", "ticker": "PFE"}]},
    }
    got = SC.universe(root, {"extra_tickers": ["acLX", "LEGN"], "exclude_tickers": ["PFE"]})
    assert got == {"LEGN": "Legend", "ACLX": ""}


def test_market_time_reads_quoted_and_base_60_times():
    assert SC.market_time("08:00").hour == 8
    assert SC.market_time(480).hour == 8  # YAML's reading of an unquoted 08:00


def test_parse_check_keeps_only_asked_tickers_and_sound_verdicts():
    reply = "Here you go:\n" + json.dumps(
        {
            "summary": "busy tape",
            "movers": [
                {
                    "ticker": "CRSP",
                    "story": True,
                    "cause": "data",
                    "worth_a_piece": True,
                    "title": "CRSP data",
                    "angle": "nope",
                    "sources": ["https://x.com/a", "ftp://b"],
                    "rank": 1,
                },
                {"ticker": "$LEGN", "story": True, "worth_a_piece": True, "title": ""},
                {"ticker": "ZZZZ", "story": True},
                {"ticker": "CRSP", "story": False},
            ],
        }
    )
    out = CK.parse_check(reply, tickers=["CRSP", "LEGN", "BEAM"], angles=["readout_reaction"])
    crsp, legn = out.verdicts
    assert crsp.worth and crsp.angle == "" and crsp.sources == ("https://x.com/a",)
    assert legn.ticker == "LEGN" and not legn.worth  # a piece needs a title
    assert legn.cause == "other"
    assert any("BEAM" in d for d in out.dropped) and any("ZZZZ" in d for d in out.dropped)
    with pytest.raises(CK.CheckRejected):
        CK.parse_check("no json", tickers=[], angles=[])


def test_due_waits_for_the_morning_and_the_interval():
    cfg = load_movers_config()
    assert runner.due(None, ny(2026, 10, 9, 7, 0), cfg)[0] is False
    assert runner.due(None, ny(2026, 10, 9, 9, 0), cfg)[0] is True
    last = M.RunRow(1, ny(2026, 10, 9, 9, 0).isoformat(), None, "done", "", "")
    assert runner.due(last, ny(2026, 10, 9, 12, 0), cfg)[0] is False
    assert runner.due(last, ny(2026, 10, 10, 9, 0), cfg)[0] is True


def _fake_fetch(symbol, params, pcfg):
    if symbol == "GONE":
        raise ValueError("404")
    daily = params["interval"] == "1d"
    if symbol == "CRSP":
        return chart(symbol, DAILY if daily else INTRADAY)
    flat_daily = [(at, 50.0, 1_000_000) for at, _, _ in DAILY]
    return chart(symbol, flat_daily if daily else [(ny(2026, 10, 8, 15, 45), 50.0, 1)])


def test_a_run_puts_a_mover_with_a_story_on_the_radar_and_queues_it(tmp_path, monkeypatch):
    from studio import store as S

    monkeypatch.setenv("DB_PATH", str(tmp_path / "pipeline.db"))
    monkeypatch.setattr(
        runner,
        "_root_config",
        lambda: {
            "companies": {
                "feeds": [
                    {"name": "CRISPR Therapeutics", "ticker": "CRSP"},
                    {"name": "Beam", "ticker": "BEAM"},
                    {"name": "Gone", "ticker": "GONE"},
                ]
            }
        },
    )
    asked = []

    def call(system, user, **kw):
        asked.append(user)
        assert kw["model"] and "WebSearch" not in user
        return json.dumps(
            {
                "summary": "one mover",
                "movers": [
                    {
                        "ticker": "CRSP",
                        "story": True,
                        "cause": "data",
                        "worth_a_piece": True,
                        "title": "CRISPR's in vivo data reset the bar",
                        "why_now": "the data hit last night",
                        "what_happened": "Phase 1 update after the close.",
                        "angle": "readout_reaction",
                        "sources": ["https://example.com/pr"],
                        "rank": 1,
                    }
                ],
            }
        )

    kw = dict(now=NOW, fetch=_fake_fetch, call=call, sleep=lambda s: None)
    assert runner.run(force=True, **kw) == 0
    assert len(asked) == 1 and "CRSP (CRISPR Therapeutics)" in asked[0]
    assert "BEAM" not in asked[0]  # flat: never sent to Claude

    conn = S.connect(tmp_path / "pipeline.db")
    topics = S.radar_topics(conn, since_iso="2000", statuses=(S.RADAR_QUEUED,))
    assert [t.title for t in topics] == ["CRISPR's in vivo data reset the bar"]
    assert topics[0].scan_id == runner.RADAR_SCAN_ID and topics[0].angle == "readout_reaction"
    queued = S.queued_topics(conn)
    assert len(queued) == 1 and queued[0].angle == ""  # the studio picks the angle
    assert "Market mover" in queued[0].topic and "+10.0% after hours" in queued[0].topic
    rows = conn.execute("SELECT session, status, radar_id FROM movers_moves").fetchall()
    assert {r["session"] for r in rows} == {"regular", "after_hours", "premarket"}
    assert {r["status"] for r in rows} == {M.STORY}
    run = M.last_run(conn)
    assert run.status == M.RUN_DONE and "GONE" in run.detail
    conn.close()

    # The same moves again: recorded already, so no second call.
    assert runner.run(force=True, **kw) == 0
    assert len(asked) == 1


def test_a_failed_check_records_no_moves_so_the_next_screen_asks_again(tmp_path, monkeypatch):
    import claude_cli

    monkeypatch.setenv("DB_PATH", str(tmp_path / "pipeline.db"))
    monkeypatch.setattr(
        runner, "_root_config", lambda: {"companies": {"feeds": [{"name": "C", "ticker": "CRSP"}]}}
    )

    def broken(*a, **k):
        raise claude_cli.ClaudeCliError("usage limit")

    kw = dict(now=NOW, fetch=_fake_fetch, sleep=lambda s: None)
    assert runner.run(force=True, call=broken, **kw) == 1
    from approval_queue import store as Q

    conn = Q.connect(tmp_path / "pipeline.db")
    assert conn.execute("SELECT COUNT(*) FROM movers_moves").fetchone()[0] == 0
    conn.close()
    calls = []
    assert runner.run(force=True, call=lambda *a, **k: calls.append(1) or "{}", **kw) == 0
    assert calls == [1]


def test_dry_run_prints_the_prompt_and_records_nothing(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "pipeline.db"))
    monkeypatch.setattr(
        runner, "_root_config", lambda: {"companies": {"feeds": [{"name": "C", "ticker": "CRSP"}]}}
    )
    assert runner.run(dry_run=True, now=NOW, fetch=_fake_fetch, sleep=lambda s: None) == 0
    assert "MOVERS" in capsys.readouterr().out
    from approval_queue import store as Q

    conn = Q.connect(tmp_path / "pipeline.db")
    M.ensure_tables(conn)
    assert conn.execute("SELECT COUNT(*) FROM movers_runs").fetchone()[0] == 0
    conn.close()
