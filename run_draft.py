"""CLI: draft every scored candidate above threshold that has no draft yet.

Usage: python run_draft.py [--min-score 30] [--since-hours 48] [--limit N] [--dry-run]
                           [--no-examples]

Drafts that pass every hard rule are stored as pending. Drafts the model could not get
past the hard rules are stored as status=failed with the reason, so they are not retried
on the next run and the reviewer can see why.

A draft that came with a chart spec (every number verified against the source) gets the chart
rendered to <db folder>/images/draft_<id>.png, unless images.enabled is false in
draft/config.yaml or matplotlib is missing (then the draft is stored without an image).

Step 7: unless --no-examples (or examples.enabled: false in draft/config.yaml), recent human
edits and rejections from the approval queue are built ONCE per run into an examples block
that goes into every draft's system prompt; draft_examples records which decisions each
draft was shown.
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import UTC, datetime, timedelta

from dotenv import load_dotenv

from approval_queue import images, store
from draft.drafter import DraftRejected, draft_item
from draft.examples import (
    EditExample,
    RejectionExample,
    format_examples_block,
    select_edit_examples,
    select_rejections,
)
from draft.schema import Draft
from draft.settings import load_draft_config

log = logging.getLogger("run_draft")


def build_examples(
    conn: store.sqlite3.Connection, cfg: dict
) -> tuple[str | None, list[EditExample], list[RejectionExample]]:
    """Select recent edits/rejections and format the block. Built once per run."""
    ex_cfg = cfg.get("examples") or {}
    now = datetime.now(UTC)
    lookback = float(ex_cfg.get("lookback_days", 60))
    rows = store.fetch_decisions_for_voice(conn, now - timedelta(days=lookback))
    edits = select_edit_examples(rows, ex_cfg, now=now)
    rejections = select_rejections(rows, ex_cfg, now=now)
    block = format_examples_block(edits, rejections)
    log.info(
        "voice examples: %d edits, %d rejections (last %d days)",
        len(edits),
        len(rejections),
        lookback,
    )
    log.debug(
        "example decision ids: edits=%s rejections=%s",
        [e.decision_id for e in edits],
        [r.decision_id for r in rejections],
    )
    return block, edits, rejections


def _default_min_score() -> float:
    """config.yaml scoring.threshold (the digest's bar), else 30 on the 0-50 scale."""
    try:
        import config as root_config

        return float((root_config.load_config().get("scoring") or {}).get("threshold", 30))
    except Exception:  # root config missing or unreadable
        return 30.0


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument(
        "--min-score",
        type=float,
        default=None,
        help="score total (0-50) a story needs to be drafted; default: config.yaml "
        "scoring.threshold",
    )
    ap.add_argument("--since-hours", type=float, default=48.0)
    ap.add_argument("--limit", type=int, default=10, help="max items to draft this run")
    ap.add_argument("--dry-run", action="store_true", help="list candidates, do not call the API")
    ap.add_argument(
        "--no-examples",
        action="store_true",
        help="do not add recent human edits/rejections to the prompt (step 7)",
    )
    ap.add_argument(
        "--retry-failed",
        action="store_true",
        help="also draft stories whose only drafts failed the hard rules",
    )
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    if args.min_score is None:
        args.min_score = _default_min_score()

    conn = store.connect()
    try:
        if not store.step1_tables_present(conn):
            log.error(
                "no items/clusters/scores tables in %s; run step 1 (run_ingest/run_score) first",
                store.db_path(),
            )
            return 1
        draft_cfg = load_draft_config()
        examples_block: str | None = None
        edits: list[EditExample] = []
        rejections: list[RejectionExample] = []
        if args.no_examples:
            log.info("voice examples: disabled by --no-examples")
        elif not (draft_cfg.get("examples") or {}).get("enabled", True):
            log.info("voice examples: disabled in draft/config.yaml")
        else:
            examples_block, edits, rejections = build_examples(conn, draft_cfg)
        edit_ids = [e.decision_id for e in edits]
        rejection_ids = [r.decision_id for r in rejections]
        if args.dry_run:
            log.info(
                "dry run: examples block is %d chars; edit decisions %s; rejection decisions %s",
                len(examples_block or ""),
                edit_ids,
                rejection_ids,
            )
        candidates = store.fetch_candidates(args.min_score, args.since_hours, conn=conn)
        todo = [
            c
            for c in candidates
            if not store.has_draft(conn, c.item_id, c.cluster_id, ignore_failed=args.retry_failed)
        ][: args.limit]
        log.info(
            "%d candidates >= %.1f in last %.0fh, %d without a draft",
            len(candidates),
            args.min_score,
            args.since_hours,
            len(todo),
        )
        drafted = failed = charts = 0
        for c in todo:
            log.info("%s %.1f %s", c.source, c.total, c.title[:80])
            if args.dry_run:
                continue
            if args.retry_failed:
                store.delete_failed_drafts(conn, c.item_id, c.cluster_id)
            try:
                result = draft_item(
                    title=c.title,
                    abstract=c.abstract,
                    url=c.url,
                    source=c.source,
                    published_at=c.published_at,
                    suggested_angle=c.suggested_angle,
                    rationale=c.rationale,
                    examples_block=examples_block,
                )
            except DraftRejected as exc:
                failed += 1
                draft_id = store.insert_draft(
                    conn,
                    item_id=c.item_id,
                    cluster_id=c.cluster_id,
                    model=exc.__class__.__name__,
                    draft=Draft("", [], "", ""),
                    status=store.STATUS_FAILED,
                    rejection_reason="; ".join(exc.reasons),
                )
                store.record_examples(conn, draft_id, edit_ids, rejection_ids)
                continue
            except Exception:
                log.exception("API failure drafting %s; will retry next run", c.item_id)
                continue
            draft_id = store.insert_draft(
                conn,
                item_id=c.item_id,
                cluster_id=c.cluster_id,
                model=result.model,
                draft=result.draft,
            )
            store.record_examples(conn, draft_id, edit_ids, rejection_ids)
            drafted += 1
            if result.flagged_numbers:
                log.warning("%s: numbers flagged for review: %s", c.item_id, result.flagged_numbers)
            if result.dropped_chart_numbers:
                log.warning(
                    "%s: chart dropped, numbers not in source: %s",
                    c.item_id,
                    result.dropped_chart_numbers,
                )
            if images.attach_chart(
                conn, draft_id, result.draft.chart, source_url=c.url, cfg=draft_cfg
            ):
                charts += 1
        log.info(
            "done: %d drafted (%d with a chart), %d failed hard rules", drafted, charts, failed
        )
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
