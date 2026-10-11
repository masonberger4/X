"""The studio's progress bar (studio/progress.py): pure, the clock a parameter."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from studio import progress as PR
from studio import store as S

NOW = datetime(2026, 10, 11, 4, 0, tzinfo=UTC)


def run(rid, stage, minutes=None, *, started=None, outcome="ok") -> S.RunRow:
    start = started or NOW - timedelta(hours=5)
    done = minutes is not None
    return S.RunRow(
        id=rid,
        piece_id=1,
        stage=stage,
        started_at=start.isoformat(),
        finished_at=(start + timedelta(minutes=minutes)).isoformat() if done else None,
        outcome=outcome if done else "",
        detail="",
        turns=10,
        cost_usd=1.0,
        duration_ms=int((minutes or 0) * 60000),
    )


def test_typical_minutes_is_the_median_of_finished_ok_runs():
    runs = [run(1, "research", 40), run(2, "research", 50), run(3, "research", 90)]
    runs += [run(4, "write", 30), run(5, "write", 300, outcome="killed")]
    assert PR.typical_minutes(runs) == {"research": 50, "write": 30}


def test_a_piece_researching_shows_where_it_is_and_what_it_is_doing():
    mine = [run(1, "research", started=NOW - timedelta(minutes=25))]
    typical = {"research": 50.0, "write": 40.0, "polish": 10.0}
    pg = PR.progress(S.STAGE_RESEARCHING, mine, typical, NOW, "opened a page\nWebFetch fda.gov\n")
    assert [s.state for s in pg.steps] == ["now", "todo", "todo", "todo"]
    assert pg.percent == 25  # half of research, a 50-minute stage of 100 in all
    assert round(pg.minutes) == 25 and pg.typical == 50.0
    assert pg.doing == "WebFetch fda.gov" and pg.stage_label == "Research"


def test_a_stage_running_long_never_fills_its_part_of_the_bar():
    mine = [run(1, "research", 50), run(2, "write", started=NOW - timedelta(minutes=200))]
    typical = {"research": 50.0, "write": 40.0, "polish": 10.0}
    pg = PR.progress(S.STAGE_WRITING, mine, typical, NOW)
    assert [s.state for s in pg.steps] == ["done", "now", "todo", "todo"]
    assert pg.percent == 88  # 50 + 0.95 * 40 of 100


def test_without_history_the_defaults_stand_in():
    pg = PR.progress(S.STAGE_POLISHING, [], {}, NOW)
    assert pg.typical == PR.DEFAULT_MINUTES["polish"] and pg.percent == 86


def test_ready_is_done_and_a_stopped_piece_has_no_bar():
    pg = PR.progress(S.STAGE_READY, [], {}, NOW)
    assert pg.percent == 100 and all(s.state == "done" for s in pg.steps)
    assert PR.progress(S.STAGE_FAILED, [], {}, NOW) is None
    waiting = PR.progress(S.STAGE_RESEARCH_READY, [], {}, NOW)
    assert waiting.stage_label == PR.WAITING and waiting.steps[0].state == "done"
