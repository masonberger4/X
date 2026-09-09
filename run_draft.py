"""CLI: draft every scored candidate above threshold that has no draft yet.

Usage: python run_draft.py [--min-score 7] [--since-hours 48] [--limit N] [--dry-run]

Drafts that pass every hard rule are stored as pending. Drafts the model could not get
past the hard rules are stored as status=failed with the reason, so they are not retried
on the next run and the reviewer can see why.
"""

from __future__ import annotations

import argparse
import logging
import sys

from dotenv import load_dotenv

from approval_queue import store
from draft.drafter import DraftRejected, draft_item
from draft.schema import Draft

log = logging.getLogger("run_draft")


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--min-score", type=float, default=7.0)
    ap.add_argument("--since-hours", type=float, default=48.0)
    ap.add_argument("--limit", type=int, default=10, help="max items to draft this run")
    ap.add_argument("--dry-run", action="store_true", help="list candidates, do not call the API")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    conn = store.connect()
    try:
        if not store.step1_tables_present(conn):
            log.error(
                "no items/clusters/scores tables in %s; run step 1 (run_ingest/run_score) first",
                store.db_path(),
            )
            return 1
        candidates = store.fetch_candidates(args.min_score, args.since_hours, conn=conn)
        todo = [c for c in candidates if not store.has_draft(conn, c.item_id, c.cluster_id)][
            : args.limit
        ]
        log.info(
            "%d candidates >= %.1f in last %.0fh, %d without a draft",
            len(candidates),
            args.min_score,
            args.since_hours,
            len(todo),
        )
        drafted = failed = 0
        for c in todo:
            log.info("%s %.1f %s", c.source, c.total, c.title[:80])
            if args.dry_run:
                continue
            try:
                result = draft_item(
                    title=c.title,
                    abstract=c.abstract,
                    url=c.url,
                    source=c.source,
                    published_at=c.published_at,
                    suggested_angle=c.suggested_angle,
                    rationale=c.rationale,
                )
            except DraftRejected as exc:
                failed += 1
                store.insert_draft(
                    conn,
                    item_id=c.item_id,
                    cluster_id=c.cluster_id,
                    model=exc.__class__.__name__,
                    draft=Draft("", [], "", ""),
                    status=store.STATUS_FAILED,
                    rejection_reason="; ".join(exc.reasons),
                )
                continue
            except Exception:
                log.exception("API failure drafting %s; will retry next run", c.item_id)
                continue
            store.insert_draft(
                conn,
                item_id=c.item_id,
                cluster_id=c.cluster_id,
                model=result.model,
                draft=result.draft,
            )
            drafted += 1
            if result.flagged_numbers:
                log.warning("%s: numbers flagged for review: %s", c.item_id, result.flagged_numbers)
        log.info("done: %d drafted, %d failed hard rules", drafted, failed)
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
