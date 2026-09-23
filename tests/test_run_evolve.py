"""run_evolve.py against a DB with step 3 posts, step 4 metrics and swarm runs. No network."""

from datetime import UTC, datetime, timedelta

import feedback.store as fstore
import publish.store as pstore
from approval_queue import store
from swarm import store as swarm_store
from swarm.genome import SEED_DESIGNERS, SEED_GENOMES, Genome

T0 = datetime(2026, 1, 1, tzinfo=UTC)


def _post(
    conn,
    draft_id,
    tweet_id,
    day,
    impressions,
    genome_id,
    winner="swarm",
    designer_id=None,
    format_id=None,
    replies=0,
    styled=False,
):
    run_id = swarm_store.record_run(
        conn,
        item_id=f"i{draft_id}",
        cluster_id=None,
        genome_id=genome_id,
        winner=winner,
        calls=1,
        log={},
        draft_id=draft_id,
        designer_id=designer_id,
        format_id=format_id,
    )
    if styled:
        swarm_store.mark_styled(conn, run_id)
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
    # a young snapshot a day after posting, then a mature one three days after: the post is
    # scored on the first snapshot at least horizon_hours (48) old, the second one
    for age, imp in ((1, impressions // 2), (3, impressions)):
        when = T0 + timedelta(days=day + age)
        conn.execute(
            "INSERT INTO tweet_metrics (tweet_id, draft_id, captured_on, captured_at, impressions,"
            " replies) VALUES (?, ?, ?, ?, ?, ?)",
            (tweet_id, draft_id, when.date().isoformat(), when.isoformat(), imp, replies),
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
        _post(conn, 300 + i, f"t{300 + i}", day, 200, w)
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
                "population_size": 3,
                "mutation_model": "",
                "smoothing": 0,
            }
        },
    )
    # breed is part of the default run; a fake strong model answers it
    import json as _json

    from swarm import mutate as _mutate

    def fake_strong(system, user, model):
        assert system == _mutate.mutation_system("impressions") and "GENOME default-6" in user
        assert "median impressions of the thread's FIRST post" in system
        return _json.dumps(
            {
                "name": "kid",
                "change": "more proposals",
                "fan_out": 9,
                "layers": 2,
                "slots": [s.__dict__ for s in SEED_GENOMES[0].slots],
            }
        )

    monkeypatch.setattr(_mutate, "call_anthropic", fake_strong)
    orig = _mutate.breed_writer
    monkeypatch.setattr(_mutate, "breed_writer", lambda *a, **k: orig(*a, call=fake_strong, **k))
    assert run_evolve.main(["--dry-run"]) == 0
    rows = swarm_store.list_fitness(conn)
    assert len(rows) == 13
    assert rows[0].relative is None and rows[3].relative == 2.0  # 2000 / median(1000 x3)
    assert {g.name for g in swarm_store.live_genomes(conn)} == set(ids)  # dry run
    out = capsys.readouterr().out
    assert "default-6              live         5" in out
    assert "wide-6                 live         5" in out
    assert "designers " in out and "house " in out
    assert "swarm won:   10 posts" in out and "control won: 0 posts" in out
    assert "credited to a writer genome: 10" in out and "Prune rule: confidence" in out

    assert run_evolve.main(["prune"]) == 0
    live = {g.name for g in swarm_store.live_genomes(conn)}
    assert live == {"default-6", "deep-4"}
    row = conn.execute("SELECT retired_reason FROM swarm_genomes WHERE id = ?", (w,)).fetchone()
    assert "P worse" in row[0] and "over 5 posts" in row[0]
    # the same data a second time retires nothing more: deep-4 has no posts to judge
    assert run_evolve.main(["prune"]) == 0
    assert {g.name for g in swarm_store.live_genomes(conn)} == {"default-6", "deep-4"}
    # breed fills the gap with a child of the best scorer, and records the parent
    assert run_evolve.main(["breed"]) == 0
    live = {g.name: g for g in swarm_store.live_genomes(conn)}
    assert set(live) == {"default-6", "deep-4", "kid"}
    assert live["kid"].parent_id == d and live["kid"].fan_out == 9
    assert live["kid"].notes == "more proposals"
    # designers had no scored posts (no designer_id on these runs): none bred without --force
    assert len(swarm_store.live_genomes(conn, "designer")) == len(SEED_DESIGNERS)
    assert run_evolve.main(["report"]) == 0
    assert "wide-6                 retired      5" in capsys.readouterr().out
    # a birth restarts the rotation: the next three stories go to three different genomes,
    # instead of the newborn taking every story until it catches up with lifetime counts
    picks = []
    for k in range(3):
        g = swarm_store.next_genome(conn)
        picks.append(g.name)
        swarm_store.record_run(
            conn, item_id=f"n{k}", cluster_id=None, genome_id=g.id, winner=None, calls=0, log=None
        )
    assert sorted(picks) == ["deep-4", "default-6", "kid"]


def test_breed_designers_with_force_and_skips_full_populations(conn, monkeypatch):
    import run_evolve

    ids = _setup(conn)
    swarm_store.retire_genome(conn, ids["wide-6"], "test")
    designers = swarm_store.live_genomes(conn, "designer")
    for d in designers:
        if d.name != "bold":  # leave one live designer, so two of three slots are gaps
            swarm_store.retire_genome(conn, d.id, "test")
    cfg = {"population_size": 3, "designer_population_size": 3, "mutation_model": "m"}
    # nothing scored, no --force: nothing bred
    assert run_evolve.cmd_breed(conn, cfg, [], dry_run=False, force=False) == []
    born = run_evolve.cmd_breed(
        conn,
        cfg,
        [],
        dry_run=False,
        force=True,
        call=lambda *a: "not json",
        rng=__import__("random").Random(1),
    )
    # the writer child failed (invalid answer) and is logged; the two designer gaps are filled
    kinds = [type(b).__name__ for b in born]
    assert kinds == ["Designer", "Designer"]
    live_d = swarm_store.live_genomes(conn, "designer")
    assert len(live_d) == 3 and all(b.parent_id == designers[2].id for b in born)
    assert len({b.name for b in born}) == 2 and all(b.name.startswith("bold-") for b in born)
    assert len(swarm_store.live_genomes(conn, "writer")) == 2
    # a dry run stores nothing
    born = run_evolve.cmd_breed(conn, cfg, [], dry_run=True, force=True, call=lambda *a: "not json")
    assert born == [] and len(swarm_store.live_genomes(conn, "designer")) == 3


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


def test_formats_are_scored_pruned_and_bred_with_their_own_bar(conn, monkeypatch):
    import run_evolve

    _setup(conn)
    formats = {f.name: f for f in swarm_store.live_genomes(conn, "format")}
    assert set(formats) == {"thread-1-first", "thread-2-ends", "thread-0", "single-1", "long-1"}
    cfg = {"population_size": 3, "format_population_size": 5, "mutation_model": "m"}
    # the population is full: nothing bred
    assert (
        run_evolve.cmd_breed(conn, cfg, [], dry_run=False, force=True, call=lambda *a: "{}") == []
    )
    swarm_store.retire_genome(conn, formats["thread-0"].id, "test")
    born = run_evolve.cmd_breed(
        conn,
        cfg,
        [],
        dry_run=False,
        force=True,
        call=lambda *a: "{}",
        rng=__import__("random").Random(4),
    )
    kinds = [type(b).__name__ for b in born]
    assert kinds == ["FormatGenome"]
    assert born[0].parent_id in {f.id for f in formats.values()}
    assert len(swarm_store.live_genomes(conn, "format")) == 5
    # a format needs format_min_posts scored posts before pruning looks at it
    from swarm import fitness

    obs = [
        fitness.Observation(
            i,
            i,
            None,
            "swarm",
            fitness.parse_when("2026-01-01T00:00:00+00:00"),
            1.0,
            1.0,
            rel,
            format_id=fid,
        )
        for i, (fid, rel) in enumerate(
            [(formats["thread-1-first"].id, 2.0)] * 6 + [(formats["single-1"].id, 0.2)] * 6
        )
    ]
    assert (
        run_evolve.cmd_prune(
            conn, {"min_posts": 5, "format_min_posts": 8, "min_alive": 2}, obs, dry_run=False
        )
        == []
    )
    obs += [
        fitness.Observation(
            99 + i,
            99 + i,
            None,
            "swarm",
            fitness.parse_when("2026-01-01T00:00:00+00:00"),
            1.0,
            1.0,
            rel,
            format_id=fid,
        )
        for i, (fid, rel) in enumerate(
            [(formats["thread-1-first"].id, 2.0)] * 2 + [(formats["single-1"].id, 0.2)] * 2
        )
    ]
    retired = run_evolve.cmd_prune(
        conn, {"min_posts": 5, "format_min_posts": 8, "min_alive": 2}, obs, dry_run=False
    )
    assert retired == [formats["single-1"].id]
    out = run_evolve.render_report(conn, {"kpi": "impressions", "baseline_days": 30}, obs)
    assert "formats " in out and "single-1" in out and "retired" in out


def _fmt_id(conn, name):
    return {f.name: f.id for f in swarm_store.live_genomes(conn, "format")}[name]


def test_writer_credit_only_for_swarm_text_and_designer_only_with_a_picture(conn, monkeypatch):
    """A control-won post, or a single post that never ran the writer's slot rules, is not
    the writer genome's work; a post whose chart was not drawn in the designer's Style (no
    picture, a table) never used the designer. swarm_fitness keeps them as account-level
    baseline but credits nobody; a run from before `styled` was tracked falls back to the
    format's picture count."""
    import run_evolve

    ids = _setup(conn)
    d = ids["default-6"]
    house = {x.name: x.id for x in swarm_store.live_genomes(conn, "designer")}["house"]
    thread, single = _fmt_id(conn, "thread-1-first"), _fmt_id(conn, "single-1")
    nopic = _fmt_id(conn, "thread-0")
    for i in range(3):
        _post(conn, 10 + i, f"w{i}", i, 100, d, designer_id=house, format_id=thread)
    kw = {"designer_id": house, "styled": True}
    _post(conn, 20, "c", 4, 100, d, winner="control", format_id=thread, **kw)
    _post(conn, 21, "s", 5, 100, d, format_id=single, **kw)
    _post(conn, 22, "n", 6, 100, d, designer_id=house, format_id=nopic)
    _post(conn, 23, "ok", 7, 100, d, format_id=thread, **kw)
    _post(conn, 24, "tab", 8, 100, d, designer_id=house, format_id=thread)  # a table
    _post(conn, 25, "old", 9, 100, d, designer_id=house, format_id=thread)
    conn.execute("UPDATE swarm_runs SET styled = NULL WHERE draft_id = 25")  # pre-tracking
    conn.commit()
    cfg = dict(run_evolve.load_swarm_config()["evolve"], kpi="impressions")
    run_evolve.cmd_score(conn, cfg)
    rows = {r.draft_id: r for r in swarm_store.list_fitness(conn)}
    assert rows[20].genome_id is None and rows[20].designer_id == house  # control's text
    assert rows[21].genome_id is None and rows[21].designer_id == house  # single post
    assert rows[22].genome_id == d and rows[22].designer_id is None  # no picture
    assert rows[23].genome_id == d and rows[23].designer_id == house
    assert rows[24].designer_id is None  # the chart was not drawn in the designer's Style
    assert rows[25].designer_id == house  # untracked: the format asked for a picture
    assert all(r.format_id is not None for r in rows.values())  # the format is always credited
    assert all(r.relative is not None for k, r in rows.items() if k >= 20)  # still baseline
    # a row the current rules no longer produce is dropped on the next score
    conn.execute("DELETE FROM posts WHERE draft_id = 23")
    conn.commit()
    run_evolve.cmd_score(conn, cfg)
    assert 23 not in {r.draft_id for r in swarm_store.list_fitness(conn)}
    assert swarm_store.fetch_winning_threads(conn, d) == []  # no drafts rows in this DB


def test_posts_are_scored_at_a_fixed_age_and_without_their_own_reply(conn):
    """A post younger than horizon_hours has no score yet; an older one is read on its first
    snapshot past the horizon, and the thread's own post 2 is not counted as a reply."""
    _setup(conn)
    g = swarm_store.live_genomes(conn)[0].id
    _post(conn, 1, "old", 0, 1000, g, replies=1)
    swarm_store.record_run(
        conn,
        item_id="y",
        cluster_id=None,
        genome_id=g,
        winner="swarm",
        calls=1,
        log={},
        draft_id=2,
    )
    posted = (T0 + timedelta(days=10)).isoformat()
    conn.execute(
        "INSERT INTO posts (draft_id, tweet_id, text, kind, position, posted_at, status)"
        " VALUES (2, 'young', 'x', 'thread', 1, ?, 'posted')",
        (posted,),
    )
    young = T0 + timedelta(days=10, hours=20)
    conn.execute(
        "INSERT INTO tweet_metrics (tweet_id, draft_id, captured_on, captured_at, impressions)"
        " VALUES ('young', 2, ?, ?, 50)",
        (young.date().isoformat(), young.isoformat()),
    )
    conn.commit()
    heads = swarm_store.fetch_head_metrics(conn, horizon_hours=48, subtract_self_reply=True)
    assert [h.tweet_id for h in heads] == ["old"]
    assert heads[0].metrics["impressions"] == 1000  # the day-3 snapshot, not the day-1 one
    assert heads[0].metrics["replies"] == 0 and heads[0].own_replies == 1
    assert heads[0].metrics["conversation"] == 0
    raw = swarm_store.fetch_head_metrics(conn)  # the old reading: newest, reply counted
    assert {h.tweet_id for h in raw} == {"old", "young"}
    assert next(h for h in raw if h.tweet_id == "old").metrics["conversation"] == 3


def test_designers_and_formats_rotate_through_every_writer(conn):
    """Each writer meets every designer in turn, instead of the lockstep rotations pairing
    writer i with designers i and i+3 forever."""
    _setup(conn)
    pairs = set()
    for k in range(36):
        g = swarm_store.next_genome(conn)
        des = swarm_store.next_designer(conn, writer_id=g.id)
        fmt = swarm_store.next_format(conn, writer_id=g.id)
        swarm_store.record_run(
            conn,
            item_id=f"p{k}",
            cluster_id=None,
            genome_id=g.id,
            winner=None,
            calls=0,
            log=None,
            designer_id=des.id,
            format_id=fmt.id,
        )
        pairs.add((g.id, des.id))
    assert len(pairs) == 3 * len(SEED_DESIGNERS)


def test_live_writers_lose_the_closer_url_rule(conn):
    """Rule 2 bans links; a genome stored before it whose closer is still the old seed
    rule word for word is rewritten in place, once, keeping its id. A closer a child
    reworded is its own gene and stays, so two genomes never collapse into one."""
    import json

    from swarm.genome import CLOSER_RULE

    swarm_store.ensure_tables(conn)
    old = SEED_GENOMES[0]
    data = json.loads(old.to_json())
    data["name"] = "legacy"
    data["slots"][-1]["rule"] = swarm_store.LEGACY_CLOSER_RULE
    reworded = dict(data, name="reworded")
    reworded["slots"] = [dict(x) for x in data["slots"]]
    reworded["slots"][-1]["rule"] = "End on the question the data leave open, then the URL."
    for d in (data, reworded):
        conn.execute(
            "INSERT INTO swarm_genomes (name, genome_json, created_at, kind)"
            " VALUES (?, ?, '2026-01-01T00:00:00+00:00', 'writer')",
            (d["name"], json.dumps(d)),
        )
    conn.commit()
    swarm_store.ensure_tables(conn)
    swarm_store.ensure_tables(conn)  # idempotent
    row = conn.execute("SELECT id, genome_json FROM swarm_genomes WHERE name = 'legacy'").fetchone()
    g = swarm_store.get_genome(conn, row[0])
    assert g.slots[-1].rule == CLOSER_RULE
    assert g.notes.count("without the source URL") == 1
    row = conn.execute("SELECT id FROM swarm_genomes WHERE name = 'reworded'").fetchone()
    assert swarm_store.get_genome(conn, row[0]).slots[-1].rule.startswith("End on the question")


def test_allocation_follows_the_scores_once_there_are_any(conn):
    """Thompson sampling: no scored post yet -> None (the caller rotates evenly); once
    scores exist the genome that did better drafts most stories and the others still get
    some, and a child starts from its parent's record."""
    import math
    import random

    ids = _setup(conn)
    kw = {"prior_sd": 0.3, "post_sd": 0.9, "min_sd": 0.3, "explore_floor": 0.1}
    rng = random.Random(3)
    assert swarm_store.thompson_next(conn, "writer", rng=rng, **kw) is None
    good, bad = ids["default-6"], ids["wide-6"]
    noise = random.Random(8)
    # noisy posts, as on X: one genome about 1.8x the average, one about 0.5x
    obs = [(good, math.exp(noise.gauss(0.6, 0.8))) for _ in range(20)]
    obs += [(bad, math.exp(noise.gauss(-0.7, 0.8))) for _ in range(20)]
    k = 0
    for gid, rel in obs:
        k += 1
        run = swarm_store.record_run(
            conn,
            item_id=f"a{k}",
            cluster_id=None,
            genome_id=gid,
            winner="swarm",
            calls=0,
            log=None,
            draft_id=1000 + k,
        )
        swarm_store.upsert_fitness(
            conn,
            run_id=run,
            draft_id=1000 + k,
            genome_id=gid,
            winner="swarm",
            tweet_id=f"a{k}",
            posted_at=f"2026-03-01T00:{k:02d}:00+00:00",
            kpi="conversation",
            value=rel,
            baseline=1.0,
            relative=rel,
        )
    picks = [swarm_store.thompson_next(conn, "writer", rng=rng, **kw).name for _ in range(400)]
    share = {n: picks.count(n) / len(picks) for n in set(picks)}
    assert share["default-6"] > 0.6 and share.get("wide-6", 0) < 0.05
    assert share.get("deep-4", 0) > 0.05  # unscored: the exploration floor still tries it
    child = Genome(
        name="kid", slots=list(SEED_GENOMES[0].slots), fan_out=7, layers=2, parent_id=good
    )
    swarm_store.insert_genome(conn, child)
    picks = [swarm_store.thompson_next(conn, "writer", rng=rng, **kw).name for _ in range(400)]
    assert picks.count("kid") > picks.count("deep-4")  # the winner's child starts ahead


def test_designers_meet_every_format_when_the_rotations_share_a_factor(conn):
    """Round-robin with as many designers as formats used to lock designer i to format i,
    so a designer paired with the no-picture format was never credited."""
    _setup(conn)
    live = swarm_store.live_genomes(conn, "designer")
    for dsg in live[5:]:
        swarm_store.retire_genome(conn, dsg.id, "test")  # 5 designers, 5 formats
    pairs = set()
    for k in range(75):
        g = swarm_store.next_genome(conn)
        fmt = swarm_store.next_format(conn, writer_id=g.id)
        des = swarm_store.next_designer(conn, writer_id=g.id, format_id=fmt.id)
        swarm_store.record_run(
            conn,
            item_id=f"q{k}",
            cluster_id=None,
            genome_id=g.id,
            winner=None,
            calls=0,
            log=None,
            designer_id=des.id,
            format_id=fmt.id,
        )
        pairs.add((des.id, fmt.id))
    assert len(pairs) == 25
