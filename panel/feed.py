"""The scored feed, as a page: what `digest.py` prints, with its rating prompt inline.

Step 1's data is read and written through `db.Database` — the same API `digest.py` uses
— never through raw SQL here. `entry_views` is pure: give it a Database and the rows
`top_scored_clusters` returned and it does no further work of its own.
"""

from __future__ import annotations

from typing import Any

from db import Cluster, Database, Score, window_start
from filter.prefilter import cluster_text

RATING_LABELS = {
    1: "1 · not worth a post",
    2: "2 · weak",
    3: "3 · fine",
    4: "4 · good",
    5: "5 · post this",
}
MAX_ABSTRACT_CHARS = 700


def feed_settings(cfg: dict[str, Any]) -> dict[str, int]:
    """Defaults for the feed page, from the root config.yaml's digest/scoring blocks."""
    dcfg = cfg.get("digest") or {}
    return {
        "top_n": int(dcfg.get("top_n", 10)),
        "hours": int(dcfg.get("window_hours", 24)),
        "threshold": int((cfg.get("scoring") or {}).get("threshold", 0)),
    }


def fetch_entries(
    db: Database, *, hours: int, top_n: int, min_total: int
) -> list[tuple[Cluster, Score]]:
    return db.top_scored_clusters(window_start(hours), top_n, min_total)


def entry_views(db: Database, rows: list[tuple[Cluster, Score]]) -> list[dict[str, Any]]:
    """One view per scored cluster: the digest entry, plus any ratings already recorded."""
    out = []
    for rank, (cl, sc) in enumerate(rows, start=1):
        items = db.items_in_cluster(cl.id)
        title, abstract = cluster_text(items) if items else (cl.title, "")
        primary = items[0] if items else None
        ratings = db.ratings_for(cl.id)
        out.append(
            {
                "rank": rank,
                "cluster_id": cl.id,
                "title": title or cl.title,
                "abstract": _clip(abstract),
                "url": primary.url if primary else None,
                "source": primary.source if primary else None,
                "doi": cl.doi,
                "published": cl.published_at.strftime("%Y-%m-%d") if cl.published_at else "n/a",
                "also_in": sorted({i.source for i in items[1:]}),
                "total": sc.total,
                "parts": [
                    ("novelty", sc.novelty),
                    ("clinical", sc.clinical_significance),
                    ("audience", sc.audience_interest),
                    ("fit", sc.expertise_fit),
                    ("timely", sc.timeliness),
                ],
                "evidence_level": sc.evidence_level,
                "hype_risk": sc.hype_risk,
                "rationale": sc.rationale,
                "suggested_angle": sc.suggested_angle,
                "model": sc.model,
                "prompt_version": sc.prompt_version,
                "human_rating": _latest(ratings, human=True),
                "model_rating": _latest(ratings, human=False),
            }
        )
    return out


def _clip(text: str) -> str:
    if len(text) <= MAX_ABSTRACT_CHARS:
        return text
    return text[:MAX_ABSTRACT_CHARS].rstrip() + "…"


def _latest(ratings: list[dict[str, Any]], *, human: bool) -> dict[str, Any] | None:
    matching = [r for r in ratings if ((r.get("rater") or "human") == "human") is human]
    return matching[-1] if matching else None


def parse_rating(raw: str | None) -> int:
    """A rating is 1-5. Anything else is a bad request, not a silent no-op."""
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        raise ValueError("rating must be a whole number from 1 to 5") from None
    if not 1 <= value <= 5:
        raise ValueError("rating must be from 1 to 5")
    return value
