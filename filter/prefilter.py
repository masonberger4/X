"""Cheap, deterministic gate applied to clusters before they cost API money.

Rules (all from config.prefilter): deny keywords, allow keywords, empty/short
abstract, and a daily cap on clusters passed to the scorer. Clusters are
processed newest-first so the cap keeps the freshest stories. Results are stored
on clusters.prefilter_status ('pass'|'drop') with a reason.

A cluster that passes the rules but hits the daily cap is DEFERRED (status stays
NULL) and is retried on the next run, so a first-day backlog drains over the
following days instead of being thrown away. A deferred cluster older than
`max_age_days` (default 7) is dropped as 'stale' instead. After a keyword change,
`run_score.py --refilter` sends every dropped cluster (except 'stale') back
through here.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from db import Database
from ingest.base import Item

log = logging.getLogger(__name__)


def cluster_text(items: list[Item]) -> tuple[str, str]:
    """(title, abstract) representing a cluster: first title, longest abstract."""
    title = items[0].title if items else ""
    abstract = max((i.abstract for i in items), key=len, default="")
    return title, abstract


def check(
    title: str, abstract: str, cfg: dict[str, Any], min_chars: int | None = None
) -> tuple[bool, str | None]:
    """Return (passes, reason). Pure function so it is trivially testable.

    `min_chars` overrides `cfg["min_abstract_chars"]` (a per-source floor)."""
    text = f"{title}\n{abstract}".lower()
    for kw in cfg.get("deny_keywords") or []:
        if kw.lower() in text:
            return False, f"deny:{kw}"
    allow = cfg.get("allow_keywords") or []
    if allow and not any(kw.lower() in text for kw in allow):
        return False, "no_allow_keyword"
    if min_chars is None:
        min_chars = int(cfg.get("min_abstract_chars", 0))
    if cfg.get("require_abstract", True) and len(abstract.strip()) < min_chars:
        return False, f"abstract<{min_chars}"
    return True, None


def _day_start(now: datetime) -> datetime:
    return now.astimezone(UTC).replace(hour=0, minute=0, second=0, microsecond=0)


def _is_stale(cl: Any, now: datetime, max_age_days: int) -> bool:
    if max_age_days <= 0:
        return False
    ref = cl.published_at or cl.created_at
    if ref is None:
        return False
    if ref.tzinfo is None:
        ref = ref.replace(tzinfo=UTC)
    return (now - ref) > timedelta(days=max_age_days)


def source_min_chars(sources: list[dict[str, Any]]) -> dict[str, int]:
    """source name -> its own `min_abstract_chars`, for sources that set one.

    Trade-press feeds carry a one-line summary, not an abstract; a source may
    lower the floor for its own items without loosening it for everyone else."""
    return {
        s["name"]: int(s["min_abstract_chars"])
        for s in sources
        if s.get("min_abstract_chars") is not None
    }


def cluster_min_chars(
    items: list[Item], cfg: dict[str, Any], overrides: dict[str, int] | None
) -> int:
    """The lowest floor among the cluster's sources (default from cfg)."""
    default = int(cfg.get("min_abstract_chars", 0))
    if not overrides:
        return default
    return min((overrides.get(i.source, default) for i in items), default=default)


def run_prefilter(
    db: Database,
    cfg: dict[str, Any],
    now: datetime | None = None,
    source_overrides: dict[str, int] | None = None,
) -> dict[str, int]:
    """Prefilter every cluster with prefilter_status IS NULL. Returns counts by reason.
    'deferred' clusters keep a NULL status and come back next run.
    `source_overrides` is `source_min_chars(cfg["sources"])`."""
    now = now or datetime.now(UTC)
    cap = int(cfg.get("daily_cap", 0) or 0)
    max_age_days = int(cfg.get("max_age_days", 7) or 0)
    passed_today = db.count_prefilter_passed_since(_day_start(now)) if cap else 0
    counts: dict[str, int] = {"pass": 0}
    for cl in db.unprefiltered_clusters():
        items = db.items_in_cluster(cl.id)
        title, abstract = cluster_text(items) if items else (cl.title, "")
        ok, reason = check(title, abstract, cfg, cluster_min_chars(items, cfg, source_overrides))
        if ok and cap and passed_today >= cap:
            if _is_stale(cl, now, max_age_days):
                ok, reason = False, "stale"
            else:
                counts["deferred"] = counts.get("deferred", 0) + 1
                log.debug("defer cluster %s (daily cap): %s", cl.id, title[:80])
                continue  # status stays NULL: retried on the next run
        if ok:
            passed_today += 1
            db.set_prefilter(cl.id, "pass", at=now)
            counts["pass"] += 1
            log.debug("pass cluster %s: %s", cl.id, title[:80])
        else:
            db.set_prefilter(cl.id, "drop", reason, at=now)
            counts[reason] = counts.get(reason, 0) + 1
            log.debug("drop cluster %s (%s): %s", cl.id, reason, title[:80])
    log.info("prefilter: %s", counts)
    return counts
