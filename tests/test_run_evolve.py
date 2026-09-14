"""run_evolve.py against a DB with step 3 posts, step 4 metrics and swarm runs. No network."""

from datetime import UTC, datetime, timedelta

import feedback.store as fstore
import publish.store as pstore
from approval_queue import store
from swarm import store as swarm_store
from swarm.genome import SEED_GENOMES

T0 = datetime(2026, 1, 1, tzinfo=UTC)


def _post(conn, draft_id, tweet_id, day, impressions, genome_id, winner="swarm"):
    run_id = swarm_store.record_run(
        conn,
        item_id=f"i{draft_id}",
        cluster_id=None,
        genome_id=genome_id,
        winner=winner,
        calls=1,
        log={},
        draft_id=draft_id,
    )
    posted = (T0 + timedelta(days=day)).isoformat()
    conn.execute(
        "INSERT INTO posts (draft_id, tweet_id, text, kind, position, posted_at, status)"
        " VALUES (?, ?, 'x', 'thread', 1, ?, 'posted')",
        (draft_id, tweet_id, posted),
    )
    conn.execute(
        "INSERT INTO posts (draft_id, tweet_id, text, kind, position, posted_at, status)"
        " VALUES (?, ?, 'y', 'thread', 2, ?, 'posted')",
        (draft_id, tweet_id + "b", posted),
    )
    for k, imp in enumerate([impressions // 2, impressions]):  # newest snapshot wins
        conn.execute(
            "INSERT INTO tweet_metrics (tweet_id, draft_id, captured_on, captured_at, impressions)"
            " VALUES (?, ?, ?, ?, ?)",
            (tweet_id, draft_id, f"2026-02-0{k + 1}", f"2026-02-0{k + 1}T00:00:00", imp),
        )
    conn.commit()
    return run_id


def _setup(conn):
    pstore._ensure_schema(conn)
    conn.executescript(fstore._SCHEMA)
    swarm_store.ensure_tables(conn)
    swarm_store.seed_default(conn)
    return {g.name: g.id for g in swarm_store.live_genomes(conn)}


def test_seed_population_and_round_robin(conn):
    ids = _setup(conn)
    assert set(ids) == {g.name for g in SEED_GENOMES}
    first = swarm_store.next_genome(conn)
    assert first.name == "default-6"
    swarm_store.record_run(
        conn, item_id="a", cluster_id=None, genome_id=first.id, winner=None, calls=0, log=None
    )
    assert swarm_store.next_genome(conn).name == "wide-6"
    swarm_store.retire_genome(conn, ids["wide-6"], "test")
    assert swarm_store.next_genome(conn).name == "deep-4"
    assert swarm_store.seed_default(conn) == ids["default-6"]  # a retired seed is not re-added
    assert {g.name for g in swarm_store.live_genomes(conn)} == {"default-6", "deep-4"}


def test_fetch_head_metrics_is_empty_without_the_other_steps_tables(conn):
    swarm_store.ensure_tables(conn)
    assert swarm_store.fetch_head_metrics(conn) == []


def test_score_prune_report(conn, monkeypatch, capsys):
    import run_evolve

    ids = _setup(conn)
    d, w = ids["default-6"], ids["wide-6"]
    # three warm-up posts, then default-6 does well and wide-6 does badly, five each
    day = 0
    for i in range(3):
        _post(conn, 100 + i, f"t{100 + i}", day, 1000, d)
        day += 1
    for i in range(5):
        _post(conn, 200 + i, f"t{200 + i}", day, 2000, d)
        _post(conn, 300 + i, f"t{300 + i}", day, 200, w, winner="control")
        day += 1
    monkeypatch.setattr(
        run_evolve,
        "load_swarm_config",
        lambda: {
            "evolve": {
                "kpi": "impressions",
                "baseline_days": 30,
                "min_baseline_posts": 3,
                "min_posts": 5,
                "min_alive": 2,
            }
        },
    )
    assert run_evolve.main(["--dry-run"]) == 0
    rows = swarm_store.list_fitness(conn)
    assert len(rows) == 13
    assert rows[0].relative is None and rows[3].relative == 2.0  # 2000 / median(1000 x3)
    assert {g.name for g in swarm_store.live_genomes(conn)} == set(ids)  # dry run
    out = capsys.readouterr().out
    assert "default-6    live         5" in out and "wide-6       live         5" in out
    assert "swarm won:   5 posts" in out and "control won: 5 posts" in out

    assert run_evolve.main(["prune"]) == 0
    live = {g.name for g in swarm_store.live_genomes(conn)}
    assert live == {"default-6", "deep-4"}
    row = conn.execute("SELECT retired_reason FROM swarm_genomes WHERE id = ?", (w,)).fetchone()
    assert "population" in row[0]
    assert run_evolve.main(["report"]) == 0
    assert "wide-6       retired      5" in capsys.readouterr().out
    # run_draft would now alternate the two survivors
    assert swarm_store.next_genome(conn).name == "deep-4"


def test_bad_kpi_exits(conn, monkeypatch):
    import pytest

    import run_evolve

    _setup(conn)
    monkeypatch.setattr(run_evolve, "load_swarm_config", lambda: {"evolve": {"kpi": "views"}})
    with pytest.raises(SystemExit):
        run_evolve.main(["score"])


def test_run_evolve_touches_only_its_own_tables(conn):
    _setup(conn)
    before = {
        t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        for t in ("posts", "tweet_metrics", "drafts", "decisions")
    }
    import run_evolve

    run_evolve.main([])
    after = {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in before}
    assert before == after
    assert isinstance(store.list_drafts(conn), list)
