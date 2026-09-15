"""Dataclasses shared by the feedback modules. No DB, no network."""

from __future__ import annotations

from dataclasses import dataclass, field, fields
from datetime import datetime

METRICS = ("impressions", "likes", "reposts", "replies", "quotes", "bookmarks")

# The ranker pays for conversation, not for reach: a reply, a quote or a bookmark is a
# much stronger signal than a like, and impressions are the OUTCOME of those signals, not
# a thing a draft can aim at. `conversation` is the weighted sum below, a derived KPI that
# can be selected on (feedback/config.yaml `kpi`, swarm/config.yaml `evolve.kpi`) exactly
# like a raw metric; it is never stored, always computed from the stored counts.
CONVERSATION = "conversation"
CONVERSATION_WEIGHTS = {
    "replies": 3.0,
    "quotes": 3.0,
    "bookmarks": 2.0,
    "reposts": 2.0,
    "likes": 1.0,
}
KPIS = (*METRICS, CONVERSATION)
DIMENSIONS = (
    "novelty",
    "clinical_significance",
    "audience_interest",
    "expertise_fit",
    "timeliness",
    "hype_risk",
)


@dataclass(frozen=True)
class Metrics:
    """public_metrics for one tweet (or a sum over several)."""

    impressions: int = 0
    likes: int = 0
    reposts: int = 0
    replies: int = 0
    quotes: int = 0
    bookmarks: int = 0

    def __add__(self, other: Metrics) -> Metrics:
        return Metrics(
            **{f.name: getattr(self, f.name) + getattr(other, f.name) for f in fields(self)}
        )

    def get(self, name: str) -> int:
        if name == CONVERSATION:
            return self.conversation
        if name not in METRICS:
            raise KeyError(f"unknown metric {name!r}")
        return int(getattr(self, name))

    @property
    def conversation(self) -> int:
        """Weighted conversation signal (CONVERSATION_WEIGHTS), rounded to an int so it
        prints and sorts like the raw counts."""
        return round(sum(w * getattr(self, n) for n, w in CONVERSATION_WEIGHTS.items()))

    @property
    def engagements(self) -> int:
        return self.likes + self.reposts + self.replies + self.quotes

    @property
    def engagement_rate(self) -> float:
        """(likes+reposts+replies+quotes)/impressions; 0.0 when there are no impressions."""
        return self.engagements / self.impressions if self.impressions > 0 else 0.0


@dataclass
class TweetMetrics:
    """What client.get_tweet_metrics returns for one requested id."""

    tweet_id: str
    metrics: Metrics = field(default_factory=Metrics)
    created_at: datetime | None = None
    deleted: bool = False


@dataclass
class UserMetrics:
    username: str
    followers: int
    following: int
    tweet_count: int


@dataclass
class PostedTweet:
    """One tweet step 3 published successfully (from its posts table)."""

    post_id: int
    draft_id: int
    tweet_id: str
    kind: str  # 'single' | 'thread'
    position: int  # 1-based within a thread; the head is the lowest position
    posted_at: datetime
    slot: str | None


@dataclass
class PostContext:
    """Everything steps 1 and 2 know about the story behind a draft."""

    draft_id: int
    item_id: str
    cluster_id: int | None
    source: str
    url: str
    title: str
    evidence_level: str
    scores: dict[str, float] = field(default_factory=dict)  # DIMENSIONS + 'total' when scored
    rating: float | None = None  # latest human rating 1-5 for the cluster
    suggested_angle: str = ""
    edited: bool = False


@dataclass
class TweetSnapshot:
    """One row of tweet_metrics."""

    tweet_id: str
    draft_id: int
    captured_on: str  # YYYY-MM-DD (UTC)
    captured_at: datetime
    metrics: Metrics = field(default_factory=Metrics)
    deleted: bool = False


@dataclass
class FollowerSnapshot:
    captured_on: str
    captured_at: datetime
    followers: int
    following: int
    tweet_count: int
