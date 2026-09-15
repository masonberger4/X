#!/usr/bin/env python
"""CLI: clear picture captions that were written to the operator, not the reader.

A chart's or table's `note` is printed under the picture, so a caption like "verify each
cell against current FDA labels before posting" — an aside meant for the reviewer — goes
out with the post. draft/chart.py:note_problems now fails such a caption at drafting time;
this command is the one-off pass over drafts that were made before it existed. For every
draft still in the queue (pending, snoozed or approved but not yet posted) it blanks the
offending caption, leaves the numbers, rows and text untouched, and re-renders the picture
so the file on disk matches. A table is redrawn through verify/render.py, with the cell
verdicts it already has; one still waiting on the fact-checker keeps no picture and is
picked up by the next `run_verify.py`.

usage: python run_scrub_notes.py [--status STATUS ...] [--dry-run] [-v]
  --status STATUS  scrub only these draft statuses (repeatable; default pending,
                   snoozed, approved)
  --dry-run        print the captions that would be cleared, change nothing
  -v               debug logging
"""

from __future__ import annotations

import argparse
import logging
import sys

from approval_queue import images
from approval_queue import store as queue_store
from draft.chart import Table, note_problems

log = logging.getLogger("run_scrub_notes")

DEFAULT_STATUSES = (
    queue_store.STATUS_PENDING,
    queue_store.STATUS_SNOOZED,
    queue_store.STATUS_APPROVED,
)
NOTE = "caption addressed the operator"


def offending_notes(row: queue_store.DraftRow) -> list[str]:
    """Every caption of the draft that reads as an aside to the operator."""
    return [v.note for v in row.draft.visuals if v.note and note_problems(v.note, "note")]


def rerender(conn, row: queue_store.DraftRow) -> None:
    """Draw the draft's pictures again from the scrubbed specs. Fail-soft: a picture that
    cannot be drawn is logged and left as it was, exactly as the drafter's own render is."""
    visual = row.draft.visual
    if isinstance(visual, Table):
        from verify.render import finalize_table, root_config
        from verify.settings import load_verify_config
        from verify.verifier import trusted_hosts

        cfg = load_verify_config()
        decision = finalize_table(conn, row, cfg=cfg, hosts=trusted_hosts(cfg, root_config()))
        log.info("draft %d: table %s", row.id, decision.status)
    elif visual is not None:
        images.attach_chart(conn, row.id, visual, source_url=row.url or "")
    if row.draft.extra_visuals:
        images.attach_extra_charts(conn, row.id, row.draft.extra_visuals, source_url=row.url or "")


def scrub(conn, statuses: list[str], dry_run: bool) -> int:
    """Returns the number of drafts whose caption was cleared (or would be)."""
    posted = set()
    done = 0
    for status in statuses:
        rows = queue_store.list_drafts(conn, status=status)
        states = queue_store.publish_states(conn, [r.id for r in rows])
        posted |= {i for i, s in states.items() if getattr(s, "posted", False)}
        for row in rows:
            notes = offending_notes(row)
            if not notes:
                continue
            if row.id in posted:
                log.info("draft %d: already posted, left alone: %s", row.id, notes)
                continue
            done += 1
            if dry_run:
                log.info("draft %d (%s): would clear %s", row.id, status, notes)
                continue
            cleared = queue_store.clear_visual_notes(conn, row.id, NOTE)
            log.info("draft %d (%s): cleared %s", row.id, status, cleared)
            fresh = queue_store.get_draft(conn, row.id)
            if fresh is not None:
                rerender(conn, fresh)
    return done


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--status", action="append", choices=list(queue_store.STATUSES))
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("-v", action="store_true")
    args = ap.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.v else logging.INFO, format="%(levelname)s %(message)s"
    )
    statuses = args.status or list(DEFAULT_STATUSES)
    conn = queue_store.connect()
    try:
        n = scrub(conn, statuses, args.dry_run)
    finally:
        conn.close()
    log.info("%d draft(s) %s", n, "would be scrubbed" if args.dry_run else "scrubbed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
