from datetime import datetime, timedelta

from swarm import fitness

T0 = datetime(2026, 1, 1)


def obs(i, value, genome=1, winner="swarm", day=None):
    return fitness.Observation(
        run_id=i,
        draft_id=i,
        genome_id=genome,
        winner=winner,
        posted_at=T0 + timedelta(days=day if day is not None else i),
        value=value,
    )


def test_score_uses_trailing_median_and_needs_enough_history():
    rows = [obs(1, 100), obs(2, 200), obs(3, 300), obs(4, 400), obs(5, 100)]
    scored = fitness.score(rows, baseline_days=30, min_baseline_posts=3)
    assert [o.relative for o in scored[:3]] == [None, None, None]
    assert scored[3].baseline == 200 and scored[3].relative == 2.0
    assert scored[4].baseline == 250 and scored[4].relative == 0.4


def test_score_window_excludes_old_posts_and_zero_baseline():
    rows = [obs(1, 0, day=0), obs(2, 0, day=1), obs(3, 0, day=2), obs(4, 50, day=3)]
    scored = fitness.score(rows, baseline_days=30, min_baseline_posts=3)
    assert scored[3].baseline == 0 and scored[3].relative is None
    rows = [obs(1, 10, day=0), obs(2, 10, day=1), obs(3, 10, day=2), obs(4, 20, day=40)]
    scored = fitness.score(rows, baseline_days=30, min_baseline_posts=3)
    assert scored[3].relative is None  # the three earlier posts fell out of the window


def test_genome_scores_and_bet_summary():
    rows = [obs(1, 1), obs(2, 1), obs(3, 1)]
    rows += [obs(4, 2, genome=1), obs(5, 4, genome=2, winner="control"), obs(6, 0.5, genome=2)]
    scored = fitness.score(rows, baseline_days=30, min_baseline_posts=3)
    gs = {s.genome_id: s for s in fitness.genome_scores(scored)}
    assert gs[1].n == 1 and gs[1].median_relative == 2.0
    assert gs[2].n == 2 and gs[2].median_relative == 2.25
    b = fitness.bet_summary(scored)
    assert (b.swarm_n, b.control_n) == (2, 1)
    assert b.swarm_median == 1.25 and b.control_median == 4.0


def _gs(gid, n, med):
    return fitness.GenomeScore(gid, n, med)


def test_prune_retires_below_population_median_but_keeps_min_alive():
    scores = [_gs(1, 6, 1.5), _gs(2, 6, 0.6), _gs(3, 6, 0.9), _gs(4, 2, 0.1)]
    d = fitness.prune(scores, [1, 2, 3, 4], min_posts=5, min_alive=2)
    assert d.population_median == 0.9 and d.retire == [2]
    assert "0.60 < population 0.90" in d.reason[2]
    d = fitness.prune(scores, [1, 2, 3, 4], min_posts=5, min_alive=4)
    assert d.retire == []
    # fewer than two genomes with enough posts: nothing to compare
    d = fitness.prune([_gs(1, 6, 1.5), _gs(2, 1, 0.1)], [1, 2], min_posts=5, min_alive=1)
    assert d.retire == [] and d.population_median is None
    # a retired genome's score never counts: with 2 gone, 3 is now the weaker of the pair
    d = fitness.prune(scores, [1, 3], min_posts=5, min_alive=1)
    assert d.retire == [3] and d.population_median == 1.2


def test_smoothing_scores_a_zero_baseline_and_keeps_the_order():
    rows = [obs(1, 0, day=0), obs(2, 0, day=1), obs(3, 0, day=2), obs(4, 3, day=3)]
    rows.append(obs(5, 1, day=4))
    scored = fitness.score(rows, baseline_days=30, min_baseline_posts=3, smoothing=1)
    assert scored[3].baseline == 0 and scored[3].relative == 4.0  # (3 + 1) / (0 + 1)
    assert scored[4].relative == 2.0  # median(0, 0, 0, 3) = 0 -> (1 + 1) / 1
    assert scored[3].relative > scored[4].relative


def test_credit_predicates():
    o = obs(1, 1)
    assert fitness.writer_credited(o) and fitness.designer_credited(o)
    assert not fitness.writer_credited(obs(2, 1, winner="control"))
    single = obs(3, 1)
    single.shape = "single"
    assert not fitness.writer_credited(single)
    nopic = obs(4, 1)
    nopic.visuals = 0
    assert not fitness.designer_credited(nopic)
    rows = fitness.score(
        [obs(i, 1) for i in range(1, 4)] + [obs(4, 2), obs(5, 2, winner="control")],
        baseline_days=30,
        min_baseline_posts=3,
    )
    gs = {s.genome_id: s for s in fitness.genome_scores(rows, include=fitness.writer_credited)}
    assert gs[1].n == 1  # the control-won post is not the genome's


def _scores(gid, rels):
    return fitness.GenomeScore(gid, len(rels), sorted(rels)[len(rels) // 2], list(rels))


def test_confident_prune_ignores_noise_and_retires_a_clear_loser():
    import math
    import random

    rng = random.Random(7)

    def draw(effect, n):
        return [math.exp(rng.gauss(math.log(effect), 0.9)) for _ in range(n)]

    # three genomes from ONE distribution, five posts each: nobody is retired
    retired = 0
    for _ in range(200):
        scores = [_scores(g, draw(1.0, 5)) for g in (1, 2, 3)]
        d = fitness.prune_confident(
            scores, [1, 2, 3], min_posts=5, min_alive=2, confidence=0.9, min_sd=0.3
        )
        retired += len(d.retire)
    assert retired / 200 < 0.2  # the median rule retires one every time
    # a genome at a quarter of the others over 20 posts each is retired, one per call
    scores = [_scores(1, draw(1.0, 20)), _scores(2, draw(1.0, 20)), _scores(3, draw(0.25, 20))]
    d = fitness.prune_confident(
        scores, [1, 2, 3], min_posts=5, min_alive=1, confidence=0.9, min_sd=0.3
    )
    assert d.retire == [3] and "P worse" in d.reason[3]
    # min_alive and ineligible genomes still hold
    d = fitness.prune_confident(
        scores, [1, 2, 3], min_posts=5, min_alive=3, confidence=0.9, min_sd=0.3
    )
    assert d.retire == []
    d = fitness.prune_confident(
        scores, [1, 3], min_posts=25, min_alive=1, confidence=0.9, min_sd=0.3
    )
    assert d.retire == [] and d.population_median is None


def test_confident_prune_floors_the_spread():
    """Five identical posts are not certainty: the spread never drops below min_sd."""
    scores = [_scores(1, [1.2] * 5), _scores(2, [1.0] * 5)]
    d = fitness.prune_confident(
        scores, [1, 2], min_posts=5, min_alive=1, confidence=0.9, min_sd=0.3
    )
    assert d.retire == []  # log(1.2) = 0.18 apart, se = 0.3 * sqrt(0.4) = 0.19
