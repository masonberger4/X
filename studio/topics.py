"""Stories from step 1, as studio topics. The studio's one read of step 1, through step 1's
own `db.Database` API (as digest.py and the panel's feed do), never raw SQL here.

`fetch_shortlist` is what an automatic piece is offered: the top scored clusters of the
last `topics.lookback_hours` that no piece has used. `fetch_story` is one cluster, for a
piece started from the feed.
"""

from __future__ import annotations

import logging
from typing import Any

from studio.prompt import Story

log = logging.getLogger(__name__)
SUMMARY_CHARS = 900


def _database() -> Any:
    from approval_queue import store as queue_store
    from db import Database

    return Database(str(queue_store.db_path()))


def _story(db: Any, cluster: Any, score: Any | None) -> Story:
    from filter.prefilter import cluster_text
    from timeutil import fmt_date

    items = db.items_in_cluster(cluster.id)
    title, abstract = cluster_text(items) if items else (cluster.title, "")
    primary = items[0] if items else None
    also = sorted({i.source for i in items[1:]})
    source = primary.source if primary else ""
    if also:
        source += f" (also {', '.join(also)})"
    return Story(
        cluster_id=int(cluster.id),
        title=title or cluster.title,
        url=(primary.url if primary else "") or "",
        source=source,
        published=fmt_date(cluster.published_at) or "",
        summary=(abstract or "")[:SUMMARY_CHARS],
        score=int(score.total) if score is not None else None,
        why=" ".join(str(score.rationale or "").split()) if score is not None else "",
    )


def fetch_shortlist(tcfg: dict[str, Any], *, exclude: set[int]) -> list[Story]:
    """Top scored stories of the lookback window, best first, minus `exclude`. Empty (and
    logged) when step 1's tables are missing or unreadable: the session then runs its own
    news scan."""
    from db import window_start

    limit = int(tcfg.get("shortlist") or 8)
    try:
        db = _database()
    except Exception as exc:  # no step-1 database yet, or it is locked for too long
        log.warning("no feed stories for the studio: %s", exc)
        return []
    try:
        rows = db.top_scored_clusters(
            window_start(int(tcfg.get("lookback_hours") or 48)),
            limit + len(exclude),
            int(tcfg.get("min_score") or 0),
        )
        out = [_story(db, cl, sc) for cl, sc in rows if int(cl.id) not in exclude]
        return out[:limit]
    except Exception as exc:
        log.warning("could not read feed stories for the studio: %s", exc)
        return []
    finally:
        db.close()


def fetch_story(cluster_id: int) -> Story | None:
    """One feed story by cluster id, with its latest score; None when it does not exist."""
    try:
        db = _database()
    except Exception as exc:
        log.warning("no feed database: %s", exc)
        return None
    try:
        cluster = db.get_cluster(int(cluster_id))
        if cluster is None:
            return None
        score = db.latest_score(int(cluster_id))
        return _story(db, cluster, score)
    finally:
        db.close()
