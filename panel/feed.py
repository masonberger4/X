"""The scored feed, as a page: what `digest.py` prints, with its editor prompt inline
(post this? yes/no, plus an explanation that starts with a reason category).

Step 1's data is read and written through `db.Database` — the same API `digest.py` uses
— never through raw SQL here. `entry_views` is pure: give it a Database and the rows
`top_scored_clusters` returned and it does no further work of its own.
"""

from __future__ import annotations

from typing import Any

from db import Cluster, Database, Score, window_start
from filter.prefilter import cluster_text
from score import editorial

DECISION_LABELS = {
    editorial.YES: "yes · post this",
    editorial.NO: "no · skip it",
}
REASON_CATEGORIES = editorial.REASON_CATEGORIES
MAX_ABSTRACT_CHARS = 700
MAX_TOP_N = 100


def feed_settings(cfg: dict[str, Any]) -> dict[str, int]:
    """Defaults for the feed page, from the root config.yaml's digest/scoring blocks."""
    dcfg = cfg.get("digest") or {}
    return {
        "top_n": clamp_top_n(int(dcfg.get("top_n", 10))),
        "hours": int(dcfg.get("window_hours", 24)),
        "threshold": int((cfg.get("scoring") or {}).get("threshold", 0)),
    }


def clamp_top_n(top_n: int) -> int:
    """The feed shows at most MAX_TOP_N stories, and never fewer than one."""
    return max(1, min(top_n, MAX_TOP_N))


def fetch_entries(
    db: Database, *, hours: int, top_n: int, min_total: int
) -> list[tuple[Cluster, Score]]:
    return db.top_scored_clusters(window_start(hours), top_n, min_total)


def entry_views(
    db: Database, rows: list[tuple[Cluster, Score]], *, hide_decided: bool = False
) -> list[dict[str, Any]]:
    """One view per scored cluster: the digest entry, plus any decisions already recorded.

    With `hide_decided`, clusters that already carry a human yes/no are left out; the
    remaining entries keep their digest rank so the numbers still match `digest.py`.
    """
    out = []
    for rank, (cl, sc) in enumerate(rows, start=1):
        ratings = db.ratings_for(cl.id)
        human = _latest(ratings, human=True)
        if hide_decided and human is not None:
            continue
        items = db.items_in_cluster(cl.id)
        title, abstract = cluster_text(items) if items else (cl.title, "")
        primary = items[0] if items else None
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
                "human_rating": human,
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
    if not matching:
        return None
    latest = dict(matching[-1])
    latest["decision"] = editorial.decision_of(latest.get("rating"))
    return latest


def parse_decision(raw: str | None) -> str:
    """'yes' or 'no'. Anything else is a bad request, not a silent no-op."""
    return editorial.parse_decision(raw)


def parse_note(raw: str | None) -> str:
    """The explanation is the training signal, so it is required."""
    note = (raw or "").strip()
    if not note:
        raise ValueError("an explanation is required: start with the deciding reason category")
    return note
