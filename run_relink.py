#!/usr/bin/env python
"""CLI: move a single or long post's source URL into a second, threaded link post.

A single or long draft used to carry the primary source URL in its own body, which made it
the one shape exempt from rule 12's link ban (draft/hook.py). Every shape now keeps its
body link-free and posts the URL as a short link post of its own; this command is the
one-off pass over the drafts that were written before that rule. For every draft still in
the queue (pending, or approved but not yet posted) whose one post carries the URL, it
takes the URL out of the body, appends "Source: <URL>" as a second post, and logs the
change as an `edit` decision holding the before and after. The draft keeps its status.

Nothing is rewritten: the split is pure text (draft/hook.py:split_link_post), there is no
model call and no network. Pictures are untouched, since they are already anchored to the
first post. A draft whose body or link post would still break the rules is left alone and
named in the log, for the reviewer to revise by hand in the queue.

usage: python run_relink.py [--status STATUS ...] [--dry-run] [-v]
  --status STATUS  relink only these draft statuses (repeatable; default pending,
                   approved)
  --dry-run        print what would change, change nothing
  -v               debug logging
"""

from __future__ import annotations

import argparse
import logging
import sys

from approval_queue import store as queue_store
from draft.hook import hook_problems, link_post_problems, split_link_post
from draft.schema import SHAPE_THREAD

log = logging.getLogger("run_relink")

DEFAULT_STATUSES = (
    queue_store.STATUS_PENDING,
    queue_store.STATUS_APPROVED,
)
NOTE = "source URL moved to its own link post"


def needs_relink(row: queue_store.DraftRow) -> bool:
    """A single or long draft still written as one post that carries the source URL."""
    return (
        (row.draft.shape or SHAPE_THREAD) != SHAPE_THREAD
        and len(row.draft.thread) == 1
        and bool(row.url)
        and row.url in row.draft.thread[0]
    )


def new_thread(row: queue_store.DraftRow) -> tuple[list[str] | None, list[str]]:
    """(the two posts, problems). The posts are None when the draft must be left alone:
    nothing to move, an empty body, or a split that would still break the rules."""
    split = split_link_post(row.draft.thread[0], row.url)
    if split is None:
        return None, ["the post is the URL and nothing else"]
    body, link = split
    problems = hook_problems(body, url=row.url, max_chars=None)
    problems += link_post_problems(link, url=row.url)
    if problems:
        return None, problems
    return [body, link], []


def relink(conn, statuses: list[str], dry_run: bool) -> int:
    """Returns the number of drafts split (or that would be)."""
    posted = set()
    done = 0
    for status in statuses:
        rows = queue_store.list_drafts(conn, status=status)
        states = queue_store.publish_states(conn, [r.id for r in rows])
        posted |= {i for i, s in states.items() if getattr(s, "posted", False)}
        for row in rows:
            if not needs_relink(row):
                continue
            if row.id in posted:
                log.info("draft %d: already posted, left alone", row.id)
                continue
            thread, problems = new_thread(row)
            if thread is None:
                log.warning("draft %d (%s): left alone: %s", row.id, status, "; ".join(problems))
                continue
            done += 1
            if dry_run:
                log.info("draft %d (%s): would become %r + %r", row.id, status, *thread)
                continue
            queue_store.edit(
                conn,
                row.id,
                thread=thread,
                note=NOTE,
                approve_after=False,
                category=queue_store.CATEGORY_HARD_RULE,
            )
            log.info("draft %d (%s): link post added", row.id, status)
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
        n = relink(conn, statuses, args.dry_run)
    finally:
        conn.close()
    log.info("%d draft(s) %s", n, "would be relinked" if args.dry_run else "relinked")
    return 0


if __name__ == "__main__":
    sys.exit(main())
