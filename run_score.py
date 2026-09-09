#!/usr/bin/env python3
"""Prefilter unfiltered clusters, then score every cluster the prefilter passed
that has no score for the configured model + prompt version."""
from __future__ import annotations

import argparse
import logging
import sys

from config import load_config, setup_logging
from db import Database
from filter.prefilter import run_prefilter
from score import rubric
from score.scorer import Scorer

log = logging.getLogger("run_score")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--config", default=None)
    ap.add_argument("--limit", type=int, default=None, help="score at most N clusters")
    ap.add_argument("--dry-run", action="store_true", help="prefilter only; list what would be scored")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)
    setup_logging(args.verbose)
    cfg = load_config(args.config)
    db = Database(cfg.get("db_path", "pipeline.db"))
    try:
        run_prefilter(db, cfg.get("prefilter") or {})
        scorer = Scorer(cfg)
        if args.dry_run:
            pending = db.unscored_clusters(scorer.model, rubric.PROMPT_VERSION)
            log.info("%d clusters pending scoring", len(pending))
            for cl in pending[: args.limit or len(pending)]:
                print(f"[{cl.id}] {cl.title[:100]}")
            return 0
        scorer.score_unscored(db, limit=args.limit)
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
