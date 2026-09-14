"""Phase two, the pure part: relative fitness, per-genome scores, the swarm-vs-control
measurement and the pruning decision. No DB, no network, no clock (`now` is never needed:
everything is relative to each post's own posted_at)."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from statistics import median


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


def parse_when(text: str) -> datetime:
    dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=None)


def score(
    obs: Sequence[Observation], *, baseline_days: float, min_baseline_posts: int
) -> list[Observation]:
    """Fill baseline and relative on every observation. The baseline is the median KPI of the
    posts in the trailing window strictly before this post; a post with fewer than
    min_baseline_posts earlier posts, or a zero baseline, gets no relative score."""
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
        o.relative = (o.value / base) if base > 0 else None
    return ordered


@dataclass
class GenomeScore:
    genome_id: int | None
    n: int
    median_relative: float | None
    values: list[float] = field(default_factory=list)


def genome_scores(obs: Iterable[Observation], key: str = "genome_id") -> list[GenomeScore]:
    """Per genome (or per designer with key='designer_id'): how many scored posts and their
    median relative KPI. Genomes with no scored post appear with n=0 so the report can show
    them."""
    by: dict[int | None, list[float]] = {}
    seen: list[int | None] = []
    for o in obs:
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
