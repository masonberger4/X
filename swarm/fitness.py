"""Phase two, the pure part: relative fitness, per-genome scores, the swarm-vs-control
measurement and the pruning decision. No DB, no network, no clock (`now` is never needed:
everything is relative to each post's own posted_at).

At a few posts a day one post's KPI is mostly the story and the day, not the genome, so a
genome is compared on the MEAN LOG of its relative scores and retired only when it is
confidently below the rest of the population (`prune_confident`); the older rule, retire
whatever sits below the population median (`prune`), stays as `evolve.prune_rule: median`.
Which genome drafts the next story is drawn by Thompson sampling on the same mean logs
(`thompson_pick`): a genome that has done better gets more stories, and one with little
evidence still gets some.
"""

from __future__ import annotations

import math
import random
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from statistics import mean, median, stdev

# A relative score of 0 (a post with nothing against a baseline with something, and no
# smoothing) has no logarithm; it counts as this instead of minus infinity.
LOG_FLOOR = 0.05


@dataclass
class Observation:
    """One posted swarm run with the head tweet's KPI value."""

    run_id: int
    draft_id: int
    genome_id: int | None
    winner: str | None
    posted_at: datetime
    value: float
    baseline: float | None = None
    relative: float | None = None
    designer_id: int | None = None
    format_id: int | None = None
    shape: str | None = None  # the run's format shape; None for a run from before phase four
    visuals: int | None = None  # how many pictures the run's format asked for
    styled: bool | None = None  # a chart was drawn in the run's designer Style (None: unknown)


def writer_credited(o: Observation) -> bool:
    """A writer genome is credited with a post only when the jury posted the swarm's draft
    and the format ran the genome's slot rules. A single post runs one fixed cell instead:
    its fan_out and layers still apply, but it is left out on purpose so the slot rules,
    which are what breeding mostly changes, are judged on posts they wrote."""
    return o.winner == "swarm" and o.shape != "single"


def designer_credited(o: Observation) -> bool:
    """A designer is credited with a post only when a chart was drawn in its Style (a table,
    a failed render or a format without a picture used none). Runs recorded before that
    was tracked fall back to whether the format asked for a picture."""
    if o.styled is not None:
        return o.styled
    return o.visuals is None or o.visuals > 0


def parse_when(text: str) -> datetime:
    dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=None)


def score(
    obs: Sequence[Observation],
    *,
    baseline_days: float,
    min_baseline_posts: int,
    smoothing: float = 0.0,
) -> list[Observation]:
    """Fill baseline and relative on every observation. The baseline is the median KPI of the
    posts in the trailing window strictly before this post, and relative is
    (value + smoothing) / (baseline + smoothing): with `smoothing` above 0 a small account's
    zero baseline still scores and a count of 1 against 0 is not an infinite win. A post
    with fewer than min_baseline_posts earlier posts, or a zero denominator, gets no
    relative score."""
    ordered = sorted(obs, key=lambda o: (o.posted_at, o.run_id))
    window = timedelta(days=baseline_days)
    for i, o in enumerate(ordered):
        earlier = [
            e.value for e in ordered[:i] if o.posted_at - window <= e.posted_at < o.posted_at
        ]
        if len(earlier) < min_baseline_posts:
            o.baseline, o.relative = None, None
            continue
        base = float(median(earlier))
        o.baseline = base
        denom = base + smoothing
        o.relative = ((o.value + smoothing) / denom) if denom > 0 else None
    return ordered


@dataclass
class GenomeScore:
    genome_id: int | None
    n: int
    median_relative: float | None
    values: list[float] = field(default_factory=list)

    @property
    def logs(self) -> list[float]:
        return [math.log(max(v, LOG_FLOOR)) for v in self.values]

    @property
    def mean_log(self) -> float | None:
        """Mean log relative: 0 is the account's average, +0.69 twice it, -0.69 half."""
        return float(mean(self.logs)) if self.values else None


def genome_scores(
    obs: Iterable[Observation],
    key: str = "genome_id",
    include: Callable[[Observation], bool] | None = None,
) -> list[GenomeScore]:
    """Per genome (or per designer with key='designer_id'): how many scored posts and their
    median relative KPI. `include` keeps only the observations the genome is credited with
    (`writer_credited`, `designer_credited`). Genomes with no scored post appear with n=0 so
    the report can show them."""
    by: dict[int | None, list[float]] = {}
    seen: list[int | None] = []
    for o in obs:
        if include is not None and not include(o):
            continue
        gid = getattr(o, key)
        if gid not in by:
            by[gid] = []
            seen.append(gid)
        if o.relative is not None:
            by[gid].append(o.relative)
    return [
        GenomeScore(g, len(by[g]), float(median(by[g])) if by[g] else None, by[g]) for g in seen
    ]


@dataclass
class BetSummary:
    """The measurement of the phase-one bet: posts the jury gave to the swarm against posts
    it gave to the control, by median relative KPI."""

    swarm_n: int
    swarm_median: float | None
    control_n: int
    control_median: float | None


def bet_summary(obs: Iterable[Observation]) -> BetSummary:
    sw = [o.relative for o in obs if o.winner == "swarm" and o.relative is not None]
    co = [o.relative for o in obs if o.winner == "control" and o.relative is not None]
    return BetSummary(
        len(sw),
        float(median(sw)) if sw else None,
        len(co),
        float(median(co)) if co else None,
    )


@dataclass
class PruneDecision:
    retire: list[int]
    reason: dict[int, str]
    population_median: float | None


def prune(
    scores: Iterable[GenomeScore], live: Sequence[int], *, min_posts: int, min_alive: int
) -> PruneDecision:
    """Which live genomes to retire: those with at least min_posts scored posts whose median
    relative KPI is below the median of every such genome. Never retires so many that fewer
    than min_alive stay live; the worst go first. A population where fewer than two genomes
    have enough posts is never pruned (nothing to compare)."""
    eligible = [
        s
        for s in scores
        if s.genome_id in live and s.n >= min_posts and s.median_relative is not None
    ]
    if len(eligible) < 2:
        return PruneDecision([], {}, None)
    pop = float(median([s.median_relative for s in eligible]))
    losers = sorted(
        (s for s in eligible if s.median_relative < pop), key=lambda s: s.median_relative
    )
    room = max(0, len(live) - min_alive)
    chosen = losers[:room]
    reasons = {
        s.genome_id: f"median relative {s.median_relative:.2f} < population {pop:.2f} "
        f"over {s.n} posts"
        for s in chosen
    }
    return PruneDecision([s.genome_id for s in chosen], reasons, pop)


def pooled_log_sd(scores: Iterable[GenomeScore], *, floor: float) -> float:
    """The per-post spread of log relative, pooled over genomes (each around its own mean),
    never below `floor`: a genome with five near-identical posts must not look certain."""
    return pooled_spread(scores, floor=floor)[0]


def pooled_spread(scores: Iterable[GenomeScore], *, floor: float) -> tuple[float, int]:
    """(spread, degrees of freedom) of log relative pooled over genomes; the spread is never
    below `floor` (nor below a hair above 0), and 0 degrees of freedom means nothing was
    measured (no genome has two posts)."""
    num = 0.0
    df = 0
    for s in scores:
        if s.n >= 2:
            num += (s.n - 1) * stdev(s.logs) ** 2
            df += s.n - 1
    sd = math.sqrt(num / df) if df > 0 else 0.0
    return max(sd, floor, 1e-6), df


def _betacf(a: float, b: float, x: float) -> float:
    """Continued fraction for the regularised incomplete beta (Lentz's method)."""
    tiny = 1e-300
    qab, qap, qam = a + b, a + 1.0, a - 1.0
    c, d = 1.0, 1.0 - qab * x / qap
    d = 1.0 / (d if abs(d) > tiny else tiny)
    h = d
    for m in range(1, 300):
        m2 = 2 * m
        aa = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1.0 + aa * d
        d = 1.0 / (d if abs(d) > tiny else tiny)
        c = 1.0 + aa / c
        c = c if abs(c) > tiny else tiny
        h *= d * c
        aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1.0 + aa * d
        d = 1.0 / (d if abs(d) > tiny else tiny)
        c = 1.0 + aa / c
        c = c if abs(c) > tiny else tiny
        delta = d * c
        h *= delta
        if abs(delta - 1.0) < 1e-12:
            break
    return h


def student_t_cdf(t: float, df: float) -> float:
    """P(T <= t) for Student's t with `df` degrees of freedom (stdlib only)."""
    if df <= 0:
        raise ValueError("df must be positive")
    x = df / (df + t * t)
    one_minus_x = t * t / (df + t * t)  # 1 - x without the rounding at tiny t
    if one_minus_x <= 0.0:
        return 0.5
    a, b = df / 2.0, 0.5
    front = math.exp(
        math.lgamma(a + b)
        - math.lgamma(a)
        - math.lgamma(b)
        + a * math.log(x)
        + b * math.log(one_minus_x)
    )
    if x < (a + 1.0) / (a + b + 2.0):
        ibeta = front * _betacf(a, b, x) / a
    else:
        ibeta = 1.0 - front * _betacf(b, a, one_minus_x) / b
    tail = 0.5 * ibeta  # P(T > |t|)
    return 1.0 - tail if t >= 0 else tail


def prune_confident(
    scores: Iterable[GenomeScore],
    live: Sequence[int],
    *,
    min_posts: int,
    min_alive: int,
    confidence: float,
    min_sd: float,
    max_retire: int = 1,
) -> PruneDecision:
    """Which live genomes to retire: among the k genomes with at least min_posts scored
    posts, those whose mean log relative is below the pooled mean of the OTHER eligible
    genomes' posts with probability at least 1 - (1 - confidence) / k (a Student t test
    on the pooled per-post spread, floored at `min_sd`; dividing by k keeps a population
    of look-alikes from losing one member to luck at every look). Nothing is retired
    while no genome has two posts, since then the spread is not measured at all. The worst
    go first, at most `max_retire` per call and never so many that fewer than min_alive
    stay live. With three genomes of five equally good posts each this retires someone
    about one look in ten, where the median rule retires someone every time; the
    probability is per look, and evolve looks again as posts accumulate.
    population_median carries the pooled mean log of every eligible post."""
    eligible = [
        s for s in scores if s.genome_id in live and s.n >= min_posts and s.mean_log is not None
    ]
    if len(eligible) < 2:
        return PruneDecision([], {}, None)
    sd, df = pooled_spread(eligible, floor=min_sd)
    total_n = sum(s.n for s in eligible)
    total_log = sum(s.n * s.mean_log for s in eligible)
    if df == 0:
        return PruneDecision([], {}, total_log / total_n)
    threshold = 1 - (1 - confidence) / len(eligible)
    losers: list[tuple[GenomeScore, float, float]] = []
    for s in eligible:
        rest_n = total_n - s.n
        rest_mean = (total_log - s.n * s.mean_log) / rest_n
        se = sd * math.sqrt(1 / s.n + 1 / rest_n)
        p_worse = student_t_cdf((rest_mean - s.mean_log) / se, df)
        if p_worse >= threshold:
            losers.append((s, p_worse, rest_mean))
    losers.sort(key=lambda t: t[0].mean_log)
    room = max(0, min(len(live) - min_alive, max_retire))
    chosen = losers[:room]
    reasons = {
        s.genome_id: f"mean log relative {s.mean_log:+.2f} vs {rest:+.2f} for the rest "
        f"(P worse {p:.2f}) over {s.n} posts"
        for s, p, rest in chosen
    }
    return PruneDecision([s.genome_id for s, _, _ in chosen], reasons, total_log / total_n)


def posterior(
    s: GenomeScore | None, *, prior_mean: float, prior_sd: float, post_sd: float
) -> tuple[float, float]:
    """(mean, sd) of a genome's true mean log relative: a normal prior (`prior_mean`,
    `prior_sd`) updated with its n scored posts, each worth a spread of `post_sd`."""
    prec = 1.0 / prior_sd**2
    if s is None or not s.n or s.mean_log is None:
        return prior_mean, prior_sd
    data_prec = s.n / post_sd**2
    mean_ = (prior_mean * prec + s.mean_log * data_prec) / (prec + data_prec)
    return mean_, math.sqrt(1.0 / (prec + data_prec))


def allocation_spread(
    scores: Iterable[GenomeScore], *, default: float, floor: float, min_df: int = 10
) -> float:
    """The per-post spread Thompson sampling assumes: the pooled measured one once there
    are `min_df` degrees of freedom, `default` before that (a small sample understates it
    and would make the sampler greedy), never below `floor`."""
    sd, df = pooled_spread(scores, floor=floor)
    return sd if df >= min_df else max(default, floor)


def thompson_pick(
    live: Sequence[int],
    scores: Mapping[int | None, GenomeScore],
    *,
    rng: random.Random,
    prior_sd: float,
    post_sd: float,
    prior_means: Mapping[int, float] | None = None,
) -> int:
    """Draw one plausible true score per live genome from its posterior and return the
    genome with the highest draw: a genome is picked as often as it is likely to be the
    best, so one that has done better gets more stories and one with little evidence
    still gets some. `prior_means` gives a genome's starting belief (a child starts from
    its parent's posterior; 0 = the account's average)."""
    if not live:
        raise ValueError("no live genome to pick")
    best_id, best_draw = live[0], -math.inf
    for gid in live:
        m0 = (prior_means or {}).get(gid, 0.0)
        mean_, sd = posterior(scores.get(gid), prior_mean=m0, prior_sd=prior_sd, post_sd=post_sd)
        draw = rng.gauss(mean_, sd)
        if draw > best_draw:
            best_id, best_draw = gid, draw
    return best_id
