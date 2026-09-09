#!/usr/bin/env python3
"""Fetch every source that is due by cadence, insert new items, and cluster them."""

from __future__ import annotations

import argparse
import logging
import sys
from typing import Any

from config import load_config, setup_logging
from db import Database
from filter.dedup import assign_cluster
from ingest.base import Source, utcnow
from ingest.biorxiv import BiorxivSource
from ingest.clinicaltrials import ClinicalTrialsSource
from ingest.crossref import CrossrefSource
from ingest.fda_oce import FDAOCESource
from ingest.pubmed import PubMedSource
from ingest.rss import RSSSource
from ingest.x_list import XListSource

log = logging.getLogger("run_ingest")

SOURCE_TYPES: dict[str, type[Source]] = {
    cls.type: cls
    for cls in (
        RSSSource,
        BiorxivSource,
        ClinicalTrialsSource,
        FDAOCESource,
        PubMedSource,
        CrossrefSource,
        XListSource,
    )
}


def build_sources(cfg: dict[str, Any]) -> list[Source]:
    out = []
    for scfg in cfg.get("sources", []):
        cls = SOURCE_TYPES.get(scfg.get("type"))
        if cls is None:
            log.warning(
                "unknown source type %r for %s; skipping", scfg.get("type"), scfg.get("name")
            )
            continue
        out.append(cls(scfg, cfg))
    return out


def ingest(
    db: Database, cfg: dict[str, Any], *, force: bool = False, only: set[str] | None = None
) -> dict[str, dict[str, int]]:
    """Run due sources. Returns {source: {fetched, inserted, clusters_new}}."""
    summary: dict[str, dict[str, int]] = {}
    now = utcnow()
    for src in build_sources(cfg):
        if only and src.name not in only:
            continue
        if not force and not src.is_due(db.last_run(src.name), now):
            log.debug("%s: not due", src.name)
            continue
        try:
            items = src.fetch()
        except Exception as exc:
            log.error("%s: fetch failed: %s", src.name, exc)
            db.record_run(src.name, 0, 0, error=str(exc)[:500])
            summary[src.name] = {"fetched": 0, "inserted": 0, "clusters_new": 0, "error": 1}
            continue
        before = db.counts()["clusters"]
        inserted = 0
        for it in items:
            ok, _ = assign_cluster(db, it, cfg.get("dedup"))
            inserted += int(ok)
        new_clusters = db.counts()["clusters"] - before
        db.record_run(src.name, len(items), inserted)
        summary[src.name] = {
            "fetched": len(items),
            "inserted": inserted,
            "clusters_new": new_clusters,
        }
        log.info(
            "%s: fetched=%d inserted=%d new_clusters=%d",
            src.name,
            len(items),
            inserted,
            new_clusters,
        )
    return summary


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--config", default=None)
    ap.add_argument("--force", action="store_true", help="ignore cadence; fetch everything")
    ap.add_argument("--source", action="append", help="only run this source (repeatable)")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)
    setup_logging(args.verbose)
    cfg = load_config(args.config)
    db = Database(cfg.get("db_path", "pipeline.db"))
    try:
        summary = ingest(db, cfg, force=args.force, only=set(args.source) if args.source else None)
    finally:
        db.close()
    tot_f = sum(s["fetched"] for s in summary.values())
    tot_i = sum(s["inserted"] for s in summary.values())
    errs = [n for n, s in summary.items() if s.get("error")]
    log.info(
        "done: %d sources run, %d fetched, %d inserted, %d errors%s",
        len(summary),
        tot_f,
        tot_i,
        len(errs),
        f" ({', '.join(errs)})" if errs else "",
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
