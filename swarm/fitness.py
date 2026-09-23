"""Phase two, the pure part: relative fitness, per-genome scores, the swarm-vs-control
measurement and the pruning decision. No DB, no network, no clock (`now` is never needed:
everything is relative to each post's own posted_at).

At a few posts a day one post's KPI is mostly the story and the day, not the genome, so a
genome is compared on the MEAN LOG of its relative scores and retired only when it is
confidently worse than the best live genome (`prune_confident`); the older rule, retire
whatever sits below the population median (`prune`), stays as `evolve.prune_rule: median`.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from statistics import NormalDist, mean, median, stdev

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


def writer_credited(o: Observation) -> bool:
    """A writer genome shaped this post only when the jury posted the swarm's draft and the
    format ran the genome's slots (a single post runs one fixed cell instead)."""
    return o.winner == "swarm" and o.shape != "single"


def designer_credited(o: Observation) -> bool:
    """A designer shaped this post only when its format carried a picture."""
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
    num = den = 0.0
    for s in scores:
        if s.n >= 2:
            num += (s.n - 1) * stdev(s.logs) ** 2
            den += s.n - 1
    sd = math.sqrt(num / den) if den > 0 else 0.0
    return max(sd, floor)


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
    genomes' posts with probability at least 1 - (1 - confidence) / k (a normal test on
    the pooled per-post spread, floored at `min_sd`; dividing by k keeps a population of
    look-alikes from losing one member to luck at every look). The worst go first, at most
    `max_retire` per call and never so many that fewer than min_alive stay live. With
    three genomes of five equally good posts each this retires someone about one look in
    seven, where the median rule retires someone every time. population_median carries
    the pooled mean log of every eligible post."""
    eligible = [
        s for s in scores if s.genome_id in live and s.n >= min_posts and s.mean_log is not None
    ]
    if len(eligible) < 2:
        return PruneDecision([], {}, None)
    sd = pooled_log_sd(eligible, floor=min_sd)
    total_n = sum(s.n for s in eligible)
    total_log = sum(s.n * s.mean_log for s in eligible)
    threshold = 1 - (1 - confidence) / len(eligible)
    dist = NormalDist()
    losers: list[tuple[GenomeScore, float, float]] = []
    for s in eligible:
        rest_n = total_n - s.n
        rest_mean = (total_log - s.n * s.mean_log) / rest_n
        se = sd * math.sqrt(1 / s.n + 1 / rest_n)
        p_worse = dist.cdf((rest_mean - s.mean_log) / se)
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
