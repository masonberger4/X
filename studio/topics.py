"""Stories from step 1, as studio topics. The studio's one read of step 1, through step 1's
own `db.Database` API (as digest.py and the panel's feed do), never raw SQL here.

`fetch_shortlist` is what an automatic piece is offered: the top scored clusters of the
last `topics.lookback_hours` that the account has not written about (the caller's
`exclude`: the studio's own stories and every story with a draft). `fetch_story` is one
cluster, for a piece started from the feed. `story_item` and `merged` follow a story that
story linking folded into another cluster.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
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


def story_item(cluster_id: int) -> str:
    """One item of a feed story (its earliest published), kept beside the story's cluster id so the
    story can be found again after linking merges it into another cluster. '' when the story
    has no items or the feed cannot be read."""
    try:
        db = _database()
    except Exception as exc:
        log.warning("no feed database: %s", exc)
        return ""
    try:
        items = db.items_in_cluster(int(cluster_id))
        return str(items[0].id) if items else ""
    except Exception as exc:
        log.warning("could not read story %s: %s", cluster_id, exc)
        return ""
    finally:
        db.close()


def merged(stories: Iterable[tuple[int, str]]) -> dict[int, int]:
    """Where story linking moved these stories: {old cluster id: the cluster that holds the
    story's item now}, for each (cluster id, item) whose cluster is gone. Linking
    (filter/link.py, db.merge_clusters) moves a cluster's items to the cluster it keeps and
    deletes the other, so the item is the trail. A story whose item is gone too, or whose
    cluster still exists, is left out; nothing when the feed cannot be read."""
    out: dict[int, int] = {}
    try:
        db = _database()
    except Exception as exc:
        log.warning("no feed database: %s", exc)
        return out
    try:
        for cluster_id, item_id in stories:
            if not item_id or db.get_cluster(int(cluster_id)) is not None:
                continue
            item = db.get_item(item_id)
            now = item.cluster_id if item is not None else None
            if now is not None and now != cluster_id and db.get_cluster(int(now)) is not None:
                out[int(cluster_id)] = int(now)
    except Exception as exc:
        log.warning("could not follow merged feed stories: %s", exc)
    finally:
        db.close()
    return out
