#!/usr/bin/env python
"""CLI: take the source URL out of the posts of drafts already in the queue.

Drafts used to carry the primary source URL in a post (the last post of a thread, or a
link post of its own after a single or long post). No post carries a link any more: a post
with an outbound link is shown to fewer readers and posting a URL is billed as an extra
request through the X API, so the source is named in words instead. This command is the
one-off pass over the drafts written before that rule. For every draft still in the queue
(pending, or approved but not yet posted) whose posts contain a link, it strips the link
(and the lead-in that only introduced it), drops a post that was nothing but the link, and
logs the change as an `edit` decision holding the before and after. The draft keeps its
status.

Nothing is rewritten: the strip is pure text (draft/hook.py:strip_links), there is no
model call and no network. Pictures are untouched unless the post they are anchored to
disappears, which cannot happen: a link-only post never carries one. A draft whose posts
would still break the rules is left alone and named in the log, for the reviewer to revise
by hand in the queue.

usage: python run_unlink.py [--status STATUS ...] [--dry-run] [-v]
  --status STATUS  unlink only these draft statuses (repeatable; default pending,
                   approved)
  --dry-run        print what would change, change nothing
  -v               debug logging
"""

from __future__ import annotations

import argparse
import logging
import sys

from approval_queue import store as queue_store
from draft.hook import HOOK_MAX_CHARS, hook_problems, link_problems, strip_links
from draft.schema import SHAPE_THREAD

log = logging.getLogger("run_unlink")

DEFAULT_STATUSES = (
    queue_store.STATUS_PENDING,
    queue_store.STATUS_APPROVED,
)
NOTE = "source URL removed: no post carries a link"


def needs_unlink(row: queue_store.DraftRow) -> bool:
    """A queued draft at least one of whose posts still carries a link."""
    return any(link_problems(post) for post in row.draft.thread)


def new_thread(row: queue_store.DraftRow) -> tuple[list[str] | None, list[str]]:
    """(the posts, problems). The posts are None when the draft must be left alone:
    nothing left to post, or a strip that would still break the rules."""
    posts: list[str] = []
    for post in row.draft.thread:
        if not link_problems(post):
            posts.append(post)
            continue
        stripped = strip_links(post)
        if stripped is not None:
            posts.append(stripped)
    if not posts:
        return None, ["every post is a link and nothing else"]
    is_thread = (row.draft.shape or SHAPE_THREAD) == SHAPE_THREAD
    problems = hook_problems(posts[0], max_chars=HOOK_MAX_CHARS if is_thread else None)
    for post in posts:
        problems += link_problems(post)
    if problems:
        return None, problems
    return posts, []


def unlink(conn, statuses: list[str], dry_run: bool) -> int:
    """Returns the number of drafts stripped (or that would be)."""
    posted = set()
    done = 0
    for status in statuses:
        rows = queue_store.list_drafts(conn, status=status)
        states = queue_store.publish_states(conn, [r.id for r in rows])
        posted |= {i for i, s in states.items() if getattr(s, "posted", False)}
        for row in rows:
            if not needs_unlink(row):
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
                log.info("draft %d (%s): would become %r", row.id, status, thread)
                continue
            queue_store.edit(
                conn,
                row.id,
                thread=thread,
                note=NOTE,
                approve_after=False,
                category=queue_store.CATEGORY_HARD_RULE,
            )
            log.info("draft %d (%s): link removed", row.id, status)
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
        n = unlink(conn, statuses, args.dry_run)
    finally:
        conn.close()
    log.info("%d draft(s) %s", n, "would be unlinked" if args.dry_run else "unlinked")
    return 0


if __name__ == "__main__":
    sys.exit(main())
