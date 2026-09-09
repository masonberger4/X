"""Cheap, deterministic gate applied to clusters before they cost API money.

Rules (all from config.prefilter): deny keywords, allow keywords, empty/short
abstract, and a daily cap on clusters passed to the scorer. Results are stored
on clusters.prefilter_status ('pass'|'drop') with a reason.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from db import Cluster, Database
from ingest.base import Item

log = logging.getLogger(__name__)


def cluster_text(items: list[Item]) -> tuple[str, str]:
    """(title, abstract) representing a cluster: first title, longest abstract."""
    title = items[0].title if items else ""
    abstract = max((i.abstract for i in items), key=len, default="")
    return title, abstract


def check(title: str, abstract: str, cfg: dict[str, Any]) -> tuple[bool, str | None]:
    """Return (passes, reason). Pure function so it is trivially testable."""
    text = f"{title}\n{abstract}".lower()
    for kw in cfg.get("deny_keywords") or []:
        if kw.lower() in text:
            return False, f"deny:{kw}"
    allow = cfg.get("allow_keywords") or []
    if allow and not any(kw.lower() in text for kw in allow):
        return False, "no_allow_keyword"
    min_chars = int(cfg.get("min_abstract_chars", 0))
    if cfg.get("require_abstract", True) and len(abstract.strip()) < min_chars:
        return False, f"abstract<{min_chars}"
    return True, None


def _day_start(now: datetime) -> datetime:
    return now.astimezone(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)


def run_prefilter(db: Database, cfg: dict[str, Any], now: datetime | None = None
                  ) -> dict[str, int]:
    """Prefilter every cluster with prefilter_status IS NULL. Returns counts by reason."""
    now = now or datetime.now(timezone.utc)
    cap = int(cfg.get("daily_cap", 0) or 0)
    passed_today = db.count_prefilter_passed_since(_day_start(now)) if cap else 0
    counts: dict[str, int] = {"pass": 0}
    for cl in db.unprefiltered_clusters():
        items = db.items_in_cluster(cl.id)
        title, abstract = cluster_text(items) if items else (cl.title, "")
        ok, reason = check(title, abstract, cfg)
        if ok and cap and passed_today >= cap:
            ok, reason = False, "daily_cap"
        if ok:
            passed_today += 1
            db.set_prefilter(cl.id, "pass")
            counts["pass"] += 1
            log.debug("pass cluster %s: %s", cl.id, title[:80])
        else:
            db.set_prefilter(cl.id, "drop", reason)
            counts[reason] = counts.get(reason, 0) + 1
            log.debug("drop cluster %s (%s): %s", cl.id, reason, title[:80])
    log.info("prefilter: %s", counts)
    return counts
