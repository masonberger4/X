"""End-to-end: run_ingest with mocked network -> prefilter -> fake scorer -> digest."""

from datetime import UTC, datetime
from pathlib import Path

import digest as digest_mod
from db import Database, Score
from filter.prefilter import run_prefilter
from ingest import http
from run_ingest import ingest

FIX = Path(__file__).parent / "fixtures"


def _cfg(tmp_path):
    return {
        "db_path": str(tmp_path / "t.db"),
        "http": {"user_agent": "t"},
        "dedup": {"title_similarity": 0.92, "near_dup_window_days": 14},
        "prefilter": {
            "require_abstract": True,
            "min_abstract_chars": 20,
            "daily_cap": 0,
            "allow_keywords": [],
            "deny_keywords": [],
        },
        "scoring": {"threshold": 20},
        "digest": {"top_n": 3, "window_hours": 24 * 365 * 5},
        "sources": [
            {
                "name": "company_regeneron",
                "type": "rss",
                "url": "https://r/rss",
                "cadence_minutes": 60,
            },
            {"name": "fda_press", "type": "rss", "url": "https://f/rss", "cadence_minutes": 60},
            {"name": "broken", "type": "rss", "url": "https://b/rss", "cadence_minutes": 60},
            {"name": "bogus", "type": "nope", "url": "https://x"},
        ],
    }


def test_ingest_cadence_errors_and_digest(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    fixtures = {"https://r/rss": "regeneron.xml", "https://f/rss": "fda_press.xml"}

    def fake_get_text(url, **kw):
        if url not in fixtures:
            raise RuntimeError("503 down")
        return (FIX / fixtures[url]).read_text()

    monkeypatch.setattr(http, "get_text", fake_get_text)
    db = Database(cfg["db_path"])
    s = ingest(db, cfg)
    assert s["company_regeneron"] == {"fetched": 10, "inserted": 10, "clusters_new": 10}
    assert s["fda_press"]["inserted"] == 20
    assert s["broken"]["error"] == 1
    assert (
        db.conn.execute("SELECT error FROM source_runs WHERE source='broken'")
        .fetchone()[0]
        .startswith("503")
    )

    # second run: nothing is due yet
    assert ingest(db, cfg) == {}
    # forced run: everything re-fetched, nothing new inserted
    s2 = ingest(db, cfg, force=True, only={"fda_press"})
    assert s2 == {"fda_press": {"fetched": 20, "inserted": 0, "clusters_new": 0}}

    counts = run_prefilter(db, cfg["prefilter"])
    assert counts["pass"] == 30

    now = datetime.now(UTC)
    for cid, total in ((1, 45), (2, 25), (3, 10), (4, 40)):
        db.insert_score(
            Score(
                cluster_id=cid,
                model="m",
                prompt_version="v1",
                novelty=9,
                clinical_significance=9,
                audience_interest=9,
                expertise_fit=9,
                timeliness=9,
                evidence_level="approval",
                hype_risk=0,
                total=total,
                rationale="why",
                suggested_angle="angle",
                raw_response="{}",
                scored_at=now,
            )
        )
    md, rows = digest_mod.build_digest(db, cfg)
    assert [sc.total for _, sc in rows] == [45, 40, 25]  # threshold 20 drops 10, top_n 3
    assert md.startswith("# Cancer research digest")
    assert "## 1. " in md and "**Score 45/50**" in md and "Angle: angle" in md

    answers = iter(["5", "great", "x", "s", "q"])
    saved = digest_mod.rate(db, rows, ask=lambda _p: next(answers))
    assert saved == 1
    assert db.ratings_for(rows[0][0].id)[0]["note"] == "great"
    db.close()


def test_auto_rate_stores_model_ratings_separately_and_shows_them(db, monkeypatch, capsys):
    """digest --auto-rate: one rating per entry tagged auto:<model>, skipped on rerun, shown as
    a hint in --rate, and never read by the feedback report as a human rating."""
    import digest as digest_mod
    from score import rater

    cfg = {
        "models": {"scorer": "m", "rater": "claude-fable-5-1", "rater_effort": "low"},
        "scoring": {"threshold": 0},
        "digest": {"top_n": 5, "window_hours": 24 * 30},
    }
    from filter.dedup import assign_cluster
    from ingest.base import Item

    now = datetime.now(UTC)
    for i in range(2):
        assign_cluster(
            db,
            Item.build(
                source="company_x",
                url=f"https://x/{i}",
                title=f"Engager readout {i}",
                abstract="a" * 300,
                published_at=now,
            ),
        )
        db.set_prefilter(i + 1, "pass")
        db.insert_score(
            Score(
                cluster_id=i + 1,
                model="m",
                prompt_version="v2",
                novelty=8,
                clinical_significance=8,
                audience_interest=8,
                expertise_fit=8,
                timeliness=8,
                evidence_level="phase2",
                hype_risk=2,
                total=39,
                rationale="why",
                suggested_angle="angle",
                raw_response="{}",
                scored_at=now,
            )
        )
    _, rows = digest_mod.build_digest(db, cfg)
    assert rows
    seen = []

    def fake_call(system, user, model, effort, cfg_):
        seen.append((model, effort))
        assert "Rating scale" in system and "TITLE:" in user
        return '```json\n{"rating": 4, "note": "Public-company readout on the beat."}\n```'

    assert digest_mod.auto_rate(db, cfg, rows, call=fake_call) == len(rows)
    assert seen[0] == ("claude-fable-5-1", "low")
    assert digest_mod.auto_rate(db, cfg, rows, call=fake_call) == 0  # idempotent per model
    r = db.ratings_for(rows[0][0].id)
    assert r[-1]["rater"] == "auto:claude-fable-5-1" and r[-1]["rating"] == 4

    answers = iter(["5", "", "q"])
    digest_mod.rate(db, rows, ask=lambda _p: next(answers))
    out = capsys.readouterr().out
    assert "model rating (auto:claude-fable-5-1): 4" in out
    human = [x for x in db.ratings_for(rows[0][0].id) if x["rater"] == "human"]
    assert human and human[-1]["rating"] == 5

    # rater.parse_reply rejects out-of-range and non-JSON replies
    import pytest

    with pytest.raises(ValueError):
        rater.parse_reply('{"rating": 7, "note": "x"}')
    with pytest.raises(ValueError):
        rater.parse_reply("no json here")
