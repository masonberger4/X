"""Clustering: DOI match -> dedup_hash match -> near-duplicate title similarity.

One cluster = one story. `assign_cluster` inserts the item (if new) and links it
to an existing cluster or creates a new one. Returns (inserted, cluster_id).
"""

from __future__ import annotations

import difflib
import logging
import re
from datetime import timedelta
from typing import Any

from db import Database
from ingest.base import Item, normalize_title, utcnow

log = logging.getLogger(__name__)


def title_similarity(a: str, b: str) -> float:
    a, b = normalize_title(a), normalize_title(b)
    if not a or not b:
        return 0.0
    return difflib.SequenceMatcher(None, a, b).ratio()


_YEAR_RE = re.compile(r"^(19|20)\d\d$")


def tokens_conflict(a: str, b: str) -> bool:
    """True when the titles differ in a real word (>=4 letters) or a year.

    Guards difflib against merging boilerplate that differs only in the entity
    ("Amgen reports Q2 results" vs "Xencor reports Q2 results")."""
    diff = set(a.split()) ^ set(b.split())
    return any((t.isalpha() and len(t) >= 4) or _YEAR_RE.match(t) for t in diff)


def find_near_duplicate(
    db: Database, norm_title: str, source: str, threshold: float, window_days: int
) -> int | None:
    """Return the id of the most similar recent cluster above threshold, if any.

    Only clusters with no member from `source` are candidates: exact repeats
    from one feed are caught by dedup_hash, and near-identical titles from the
    same feed are almost always recurring boilerplate, not the same story."""
    if not norm_title:
        return None
    since = utcnow() - timedelta(days=window_days)
    best_id, best = None, 0.0
    for cl in db.recent_clusters(since):
        if source in cl.sources:
            continue
        # cheap length gate before SequenceMatcher
        la, lb = len(cl.norm_title), len(norm_title)
        if not la or min(la, lb) / max(la, lb) < threshold - 0.05:
            continue
        r = difflib.SequenceMatcher(None, cl.norm_title, norm_title).ratio()
        if r > best and not tokens_conflict(cl.norm_title, norm_title):
            best_id, best = cl.id, r
    if best_id is not None and best >= threshold:
        log.debug("near-dup: %r ~ cluster %s (%.3f)", norm_title[:60], best_id, best)
        return best_id
    return None


def assign_cluster(
    db: Database, item: Item, dedup_cfg: dict[str, Any] | None = None
) -> tuple[bool, int | None]:
    """Insert item and attach it to a cluster.

    Order: (1) exact dedup_hash -> not inserted; (2) DOI match -> join cluster;
    (3) near-duplicate normalised title -> join cluster; else new cluster.
    """
    dedup_cfg = dedup_cfg or {}
    threshold = float(dedup_cfg.get("title_similarity", 0.92))
    window_days = int(dedup_cfg.get("near_dup_window_days", 14))

    if db.item_exists(item.dedup_hash):
        log.debug("skip (seen): %s", item.title[:80])
        return False, None

    cluster_id: int | None = None
    if item.doi:
        cl = db.find_cluster_by_doi(item.doi)
        if cl:
            cluster_id = cl.id
            log.debug("doi match -> cluster %s: %s", cluster_id, item.title[:80])

    norm = normalize_title(item.title)
    if cluster_id is None:
        cluster_id = find_near_duplicate(db, norm, item.source, threshold, window_days)

    if cluster_id is None:
        cluster_id = db.create_cluster(item.title, norm, item.doi, item.published_at)
        log.debug("new cluster %s: %s", cluster_id, item.title[:80])
    else:
        db.update_cluster_published(cluster_id, item.published_at, item.doi)

    item.cluster_id = cluster_id
    inserted = db.insert_item(item)
    return inserted, cluster_id
