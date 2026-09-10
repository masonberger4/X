"""Story linking (filter/link.py): the model proposes groups, code validates and merges."""

from datetime import UTC, datetime, timedelta

import pytest

from db import Score
from filter import link
from filter.dedup import assign_cluster
from ingest.base import Item
from score import rubric

CFG = {"models": {"linker": "link-model"}, "linking": {"window_hours": 48}}


def _add(db, source, title, hours_ago=1, abstract="a" * 50):
    it = Item.build(
        source=source,
        url=f"https://{source}/{abs(hash(title))}",
        title=title,
        abstract=abstract,
        published_at=datetime.now(UTC) - timedelta(hours=hours_ago),
    )
    _, cid = assign_cluster(db, it)
    db.set_prefilter(cid, "pass")
    return cid


def _score(db, cid, total=40):
    db.insert_score(
        Score(
            cluster_id=cid,
            model="m",
            prompt_version=rubric.PROMPT_VERSION,
            novelty=8,
            clinical_significance=8,
            audience_interest=8,
            expertise_fit=8,
            timeliness=8,
            evidence_level="phase 3",
            hype_risk=0,
            total=total,
            rationale="r",
            suggested_angle="a",
            raw_response="{}",
            scored_at=datetime.now(UTC),
        )
    )


def test_parse_reply_exact():
    assert link.parse_reply('{"groups": [[1, 2, 3], [2, 4], [5], [9, 1]]}', {1, 2, 3, 4, 5}) == [
        [1, 2, 3]
    ]
    assert link.parse_reply('{"groups": []}', {1}) == []
    with pytest.raises(ValueError):
        link.parse_reply("no json here", {1})
    with pytest.raises(ValueError):
        link.parse_reply('{"nope": 1}', {1})


def test_merge_clusters_moves_rows_and_keeps_earliest_date(db):
    a = _add(db, "company_kura", "Kura creates Lilly-backed spinout", hours_ago=30)
    b = _add(db, "fierce_biotech", "Kura spins diabetes work into a startup", hours_ago=2)
    db.insert_rating(b, 5, "yes")
    _score(db, b)
    moved = db.merge_clusters(a, [b, a, 999])
    assert moved == 1
    assert db.get_cluster(b) is None
    kept = db.get_cluster(a)
    assert sorted(kept.sources) == ["company_kura", "fierce_biotech"]
    assert kept.published_at < datetime.now(UTC) - timedelta(hours=29)
    assert db.has_score(a) and db.ratings_for(a)[0]["rating"] == 5
    assert db.merge_clusters(a, []) == 0
    with pytest.raises(ValueError):
        db.merge_clusters(999, [a])


def test_link_recent_merges_groups_into_the_scored_cluster(db):
    release = _add(db, "company_kura", "Kura Oncology forms Lilly-backed spinout", hours_ago=20)
    _score(db, release)
    fierce = _add(db, "fierce_biotech", "Kura creates Lilly-backed spinout", hours_ago=3)
    dive = _add(db, "biopharma_dive", "Kura spins diabetes work into a startup", hours_ago=2)
    other = _add(db, "fierce_biotech", "Biohaven drug suffers FDA hold", hours_ago=1)
    old = _add(db, "nejm", "Ancient paper", hours_ago=200)
    db.conn.execute(  # arrived long ago too: outside the window
        "UPDATE clusters SET created_at = ? WHERE id = ?",
        ((datetime.now(UTC) - timedelta(hours=200)).isoformat(), old),
    )
    db.conn.commit()
    seen = {}

    def fake(system, user, model, effort, cfg):
        seen.update(system=system, user=user, model=model, effort=effort)
        return f'{{"groups": [[{fierce}, {dive}, {release}], [{old}, {other}]]}}'

    res = link.link_recent(db, CFG, call=fake)
    assert seen["model"] == "link-model" and f"[{other}]" in seen["user"]
    assert f"[{old}]" not in seen["user"], "outside the window"
    assert res.candidates == 4 and res.merged == 2 and res.error is None
    assert res.groups == [[fierce, dive, release]]
    assert db.get_cluster(fierce) is None and db.get_cluster(dive) is None
    kept = db.get_cluster(release)
    assert sorted(kept.sources) == ["biopharma_dive", "company_kura", "fierce_biotech"]
    assert db.get_cluster(other) is not None
    # the merged story is not scored again
    assert [c.id for c in db.unscored_clusters("m", rubric.PROMPT_VERSION)] == [other, old]


def test_link_recent_fails_soft_and_honours_config(db):
    a = _add(db, "s1", "Story one")
    b = _add(db, "s2", "Story one again")

    def boom(*a, **k):
        raise RuntimeError("model down")

    res = link.link_recent(db, CFG, call=boom)
    assert res.error.startswith("RuntimeError") and res.merged == 0
    assert db.get_cluster(a) and db.get_cluster(b)

    res = link.link_recent(db, {"models": {}, "linking": {}}, call=boom)
    assert "models.linker" in res.error
    res = link.link_recent(db, {**CFG, "linking": {"enabled": False}}, call=boom)
    assert res.candidates == 0 and res.error is None


def test_link_recent_needs_two_candidates(db):
    _add(db, "s1", "Only story")
    calls = []
    res = link.link_recent(db, CFG, call=lambda *a: calls.append(a) or '{"groups": []}')
    assert res.candidates == 1 and calls == []


def test_run_score_links_before_scoring_unless_told_not_to(db, monkeypatch, tmp_path):
    import run_score

    calls = []
    fake_cfg = {**CFG, "db_path": ":memory:", "sources": []}
    monkeypatch.setattr(run_score, "load_config", lambda p: fake_cfg)
    monkeypatch.setattr(run_score.link, "link_recent", lambda d, c: calls.append("link"))
    monkeypatch.setattr(
        run_score,
        "Scorer",
        lambda cfg: type(
            "S",
            (),
            {
                "score_unscored": lambda self, db, limit=None: [],
                "model": "m",
            },
        )(),
    )
    monkeypatch.setattr(run_score, "run_prefilter", lambda *a, **k: {})
    monkeypatch.setattr(run_score, "source_min_chars", lambda s: {})
    assert run_score.main(["--config", "x"]) == 0
    assert run_score.main(["--config", "x", "--no-link"]) == 0
    assert run_score.main(["--config", "x", "--dry-run"]) == 0
    assert calls == ["link"]
