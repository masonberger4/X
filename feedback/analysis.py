"""Pure analysis over the feedback dataclasses. No DB, no network, no scipy.

Conventions:
- A "post" is one draft as published: a single tweet, or a thread whose head is the tweet
  with the lowest position. Group comparisons and correlations use the HEAD's metrics so
  singles and threads are comparable; reply-tweet metrics are summed and shown separately.
- The KPI (config `kpi`, default impressions) is always read from the head.
- Small groups are never dropped here; the report and suggest layers decide what to show.
"""

from __future__ import annotations

import statistics
from collections import defaultdict
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from zoneinfo import ZoneInfo

from feedback.models import (
    DIMENSIONS,
    FollowerSnapshot,
    Metrics,
    PostContext,
    PostedTweet,
    TweetSnapshot,
)

GROUP_DIMENSIONS = ("source", "evidence_level", "kind", "slot", "edited", "topic", "hour")
CORRELATION_NAMES = (*DIMENSIONS, "total", "rating")


@dataclass
class PostRow:
    draft_id: int
    tweet_id: str
    posted_at: datetime
    kind: str
    slot: str  # 'HH:MM' slot label or 'off-slot'
    hour: int  # local hour posted (config timezone)
    source: str
    evidence_level: str
    edited: bool
    topics: list[str]
    scores: dict[str, float]
    rating: float | None
    title: str
    url: str
    suggested_angle: str
    head: Metrics
    reply_sum: Metrics = field(default_factory=Metrics)  # sum over reply tweets (threads)
    reply_count: int = 0  # reply tweets that had a snapshot
    reply_deleted: int = 0

    @property
    def total(self) -> Metrics:
        return self.head + self.reply_sum

    def kpi(self, name: str) -> int:
        return self.head.get(name)

    @property
    def is_thread(self) -> bool:
        return self.kind == "thread"


@dataclass
class GroupSummary:
    dimension: str
    group: str
    n: int
    median_kpi: float
    mean_kpi: float
    engagement_rate: float  # pooled over the group's head tweets


@dataclass
class Correlation:
    name: str
    n: int
    rho: float | None  # None when fewer than 3 pairs or no variance


@dataclass
class FollowerDelta:
    start: FollowerSnapshot | None
    end: FollowerSnapshot | None

    @property
    def delta(self) -> int | None:
        """None (not 0) unless two distinct snapshots exist."""
        if self.start is None or self.end is None or self.start is self.end:
            return None
        return self.end.followers - self.start.followers


@dataclass
class Analysis:
    window_start: datetime
    window_end: datetime
    kpi: str
    rows: list[PostRow]
    groups: dict[str, list[GroupSummary]]
    correlations: list[Correlation]
    followers: FollowerDelta
    top: list[PostRow]
    bottom: list[PostRow]

    @property
    def n_posts(self) -> int:
        return len(self.rows)

    @property
    def n_threads(self) -> int:
        return sum(1 for r in self.rows if r.is_thread)

    @property
    def head_total(self) -> Metrics:
        return sum((r.head for r in self.rows), Metrics())

    @property
    def reply_total(self) -> Metrics:
        return sum((r.reply_sum for r in self.rows), Metrics())

    @property
    def median_kpi(self) -> float | None:
        return median([r.kpi(self.kpi) for r in self.rows])


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def median(values: Sequence[float]) -> float | None:
    return float(statistics.median(values)) if values else None


def mean(values: Sequence[float]) -> float | None:
    return float(statistics.fmean(values)) if values else None


def latest_snapshots(snapshots: Iterable[TweetSnapshot]) -> dict[str, TweetSnapshot]:
    """The most recent snapshot per tweet_id (by captured_on, then captured_at)."""
    latest: dict[str, TweetSnapshot] = {}
    for s in snapshots:
        cur = latest.get(s.tweet_id)
        if cur is None or (s.captured_on, s.captured_at) > (cur.captured_on, cur.captured_at):
            latest[s.tweet_id] = s
    return latest


def slot_key(slot: str | None) -> str:
    """'2026-03-08 08:30' -> '08:30'; None/empty -> 'off-slot' (breaking or --now posts)."""
    if not slot:
        return "off-slot"
    return slot.split()[-1]


def tag_topics(title: str, topics: dict[str, list[str]] | None) -> list[str]:
    """Topic names whose keywords appear in the title (case-insensitive)."""
    if not topics:
        return []
    low = (title or "").lower()
    return [name for name, kws in topics.items() if any(k.lower() in low for k in kws)]


# ---------------------------------------------------------------------------
# Rows
# ---------------------------------------------------------------------------


def build_rows(
    posted: Iterable[PostedTweet],
    contexts: dict[int, PostContext],
    latest: dict[str, TweetSnapshot],
    *,
    timezone: str = "UTC",
    topics: dict[str, list[str]] | None = None,
) -> list[PostRow]:
    """One PostRow per draft whose head tweet has a live (non-deleted) snapshot.

    Thread metrics: the head's own metrics plus the sum over reply tweets that have a
    snapshot; deleted replies are counted, not summed.
    """
    tz = ZoneInfo(timezone)
    by_draft: dict[int, list[PostedTweet]] = defaultdict(list)
    for p in posted:
        by_draft[p.draft_id].append(p)
    rows: list[PostRow] = []
    for draft_id, tweets in by_draft.items():
        tweets.sort(key=lambda t: (t.position, t.post_id))
        head = tweets[0]
        snap = latest.get(head.tweet_id)
        if snap is None or snap.deleted:
            continue
        ctx = contexts.get(draft_id) or PostContext(
            draft_id=draft_id,
            item_id="",
            cluster_id=None,
            source="",
            url="",
            title="",
            evidence_level="",
        )
        reply_sum = Metrics()
        reply_count = reply_deleted = 0
        for t in tweets[1:]:
            s = latest.get(t.tweet_id)
            if s is None:
                continue
            if s.deleted:
                reply_deleted += 1
                continue
            reply_sum = reply_sum + s.metrics
            reply_count += 1
        rows.append(
            PostRow(
                draft_id=draft_id,
                tweet_id=head.tweet_id,
                posted_at=head.posted_at,
                kind=head.kind,
                slot=slot_key(head.slot),
                hour=head.posted_at.astimezone(tz).hour,
                source=ctx.source or "unknown",
                evidence_level=ctx.evidence_level or "unknown",
                edited=ctx.edited,
                topics=tag_topics(ctx.title, topics),
                scores=dict(ctx.scores),
                rating=ctx.rating,
                title=ctx.title,
                url=ctx.url,
                suggested_angle=ctx.suggested_angle,
                head=snap.metrics,
                reply_sum=reply_sum,
                reply_count=reply_count,
                reply_deleted=reply_deleted,
            )
        )
    rows.sort(key=lambda r: (r.posted_at, r.draft_id))
    return rows


# ---------------------------------------------------------------------------
# Groups
# ---------------------------------------------------------------------------


def _group_keys(row: PostRow, dimension: str) -> list[str]:
    if dimension == "source":
        return [row.source]
    if dimension == "evidence_level":
        return [row.evidence_level]
    if dimension == "kind":
        return [row.kind]
    if dimension == "slot":
        return [row.slot]
    if dimension == "edited":
        return ["edited" if row.edited else "unedited"]
    if dimension == "topic":
        return list(row.topics) or ["untagged"]
    if dimension == "hour":
        return [f"{row.hour:02d}:00"]
    raise KeyError(f"unknown group dimension {dimension!r}")


def group_rows(rows: Iterable[PostRow], dimension: str) -> dict[str, list[PostRow]]:
    """Rows keyed by group value. A row can belong to several topics."""
    out: dict[str, list[PostRow]] = defaultdict(list)
    for r in rows:
        for k in _group_keys(r, dimension):
            out[k].append(r)
    return dict(out)


def summarise(dimension: str, group: str, rows: Sequence[PostRow], kpi: str) -> GroupSummary:
    values = [r.kpi(kpi) for r in rows]
    pooled = sum((r.head for r in rows), Metrics())
    return GroupSummary(
        dimension=dimension,
        group=group,
        n=len(rows),
        median_kpi=median(values) or 0.0,
        mean_kpi=mean(values) or 0.0,
        engagement_rate=pooled.engagement_rate,
    )


def group_summaries(rows: Sequence[PostRow], dimension: str, kpi: str) -> list[GroupSummary]:
    """Median + mean KPI and pooled engagement rate per group, best median first."""
    groups = group_rows(rows, dimension)
    out = [summarise(dimension, g, rs, kpi) for g, rs in groups.items()]
    out.sort(key=lambda s: (-s.median_kpi, -s.n, s.group))
    return out


def compare_group(
    rows: Sequence[PostRow], dimension: str, group: str, kpi: str
) -> tuple[int, float | None, int, float | None]:
    """(n_in, median_in, n_out, median_out): the group against every other row."""
    inside = [r for r in rows if group in _group_keys(r, dimension)]
    outside = [r for r in rows if group not in _group_keys(r, dimension)]
    return (
        len(inside),
        median([r.kpi(kpi) for r in inside]),
        len(outside),
        median([r.kpi(kpi) for r in outside]),
    )


# ---------------------------------------------------------------------------
# Correlation
# ---------------------------------------------------------------------------


def _ranks(values: Sequence[float]) -> list[float]:
    """Fractional (average) ranks, 1-based, ties share the mean of their positions."""
    order = sorted(range(len(values)), key=lambda i: values[i])
    ranks = [0.0] * len(values)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
            j += 1
        avg = (i + j) / 2 + 1
        for k in range(i, j + 1):
            ranks[order[k]] = avg
        i = j + 1
    return ranks


def spearman(xs: Sequence[float], ys: Sequence[float]) -> float | None:
    """Spearman rank correlation with average ranks for ties (Pearson on ranks).

    None if fewer than 3 pairs or either side has no variance.
    """
    if len(xs) != len(ys):
        raise ValueError("xs and ys must have the same length")
    n = len(xs)
    if n < 3:
        return None
    rx, ry = _ranks(xs), _ranks(ys)
    mx, my = sum(rx) / n, sum(ry) / n
    sxx = sum((a - mx) ** 2 for a in rx)
    syy = sum((b - my) ** 2 for b in ry)
    if sxx == 0 or syy == 0:
        return None
    sxy = sum((a - mx) * (b - my) for a, b in zip(rx, ry, strict=True))
    return sxy / (sxx * syy) ** 0.5


def correlations(rows: Sequence[PostRow], kpi: str) -> list[Correlation]:
    """Spearman rho between each score dimension / total / human rating and the KPI."""
    out: list[Correlation] = []
    for name in CORRELATION_NAMES:
        pairs: list[tuple[float, float]] = []
        for r in rows:
            v = r.rating if name == "rating" else r.scores.get(name)
            if v is None:
                continue
            pairs.append((float(v), float(r.kpi(kpi))))
        rho = spearman([p[0] for p in pairs], [p[1] for p in pairs]) if pairs else None
        out.append(Correlation(name=name, n=len(pairs), rho=rho))
    return out


# ---------------------------------------------------------------------------
# Followers, ranking, whole analysis
# ---------------------------------------------------------------------------


def follower_delta(snapshots: Iterable[FollowerSnapshot]) -> FollowerDelta:
    """Earliest and latest snapshot in the window; delta is None with fewer than two."""
    ordered = sorted(snapshots, key=lambda s: (s.captured_on, s.captured_at))
    if not ordered:
        return FollowerDelta(None, None)
    if len(ordered) == 1:
        return FollowerDelta(ordered[0], ordered[0])
    return FollowerDelta(ordered[0], ordered[-1])


def rank_rows(rows: Sequence[PostRow], kpi: str, n: int = 5) -> tuple[list[PostRow], list[PostRow]]:
    """(top n, bottom n) by KPI; ties broken by engagement then recency."""
    ordered = sorted(
        rows, key=lambda r: (r.kpi(kpi), r.head.engagements, r.posted_at), reverse=True
    )
    top = ordered[:n]
    bottom = list(reversed(ordered[-n:])) if len(ordered) > n else list(reversed(ordered))
    return top, bottom


def analyse(
    posted: Iterable[PostedTweet],
    contexts: dict[int, PostContext],
    tweet_snapshots: Iterable[TweetSnapshot],
    follower_snapshots: Iterable[FollowerSnapshot],
    *,
    window_start: datetime,
    window_end: datetime,
    kpi: str = "impressions",
    timezone: str = "UTC",
    topics: dict[str, list[str]] | None = None,
    top_n: int = 5,
    row_builder: Callable[..., list[PostRow]] = build_rows,
) -> Analysis:
    latest = latest_snapshots(tweet_snapshots)
    rows = row_builder(posted, contexts, latest, timezone=timezone, topics=topics)
    top, bottom = rank_rows(rows, kpi, top_n)
    return Analysis(
        window_start=window_start,
        window_end=window_end,
        kpi=kpi,
        rows=rows,
        groups={d: group_summaries(rows, d, kpi) for d in GROUP_DIMENSIONS},
        correlations=correlations(rows, kpi),
        followers=follower_delta(follower_snapshots),
        top=top,
        bottom=bottom,
    )
