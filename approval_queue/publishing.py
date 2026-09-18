"""What the queue needs from step 3 when a human takes a draft back, or puts it back in line.

Reopening an approved draft, and releasing one whose publish attempt ended without a
tweet, are the two places step 2 writes into step 3's tables, and both write only through
`publish/store.py` (`forget`, `release_failed`, `release_claimed`), which refuses anything
live. Everything else the queue knows about publishing it reads through
`store.publish_states`. Keeping these calls here rather than in a route mirrors
`panel/publishing.py`, so the carve-out is stated in one place instead of being spelled out
at the call site.

A release differs from a reopen: the draft stays approved and keeps its place in the
publishing order, only step 3's dead schedule row goes, so the next run considers it again.
`release_reason` is the one gate, read by both the button and the route.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime

from publish import store as publish_store

STALE_CLAIM_MINUTES = publish_store.STALE_CLAIM_MINUTES

#: Schedule states a human may release a draft from: the attempt is over and posted
#: nothing ('failed', 'refused'), or the run that claimed it died without resolving the
#: claim ('claimed', and only once the claim is stale). 'pending' needs no release, and
#: 'posted'/'partial' are live.
RELEASABLE_STATES = (publish_store.SCHED_FAILED, publish_store.SCHED_REFUSED)

_LIVE = (
    "this draft is already live on X and cannot be reopened; reject it instead if it "
    "should not run again"
)
_LIVE_RELEASE = (
    "this draft is already live on X; releasing it would post it twice. Reject it instead "
    "if it should not run again"
)
_NOT_HELD = "publishing is not holding this draft: it is already waiting for the next run"
_FRESH_CLAIM = (
    f"a publish run claimed this draft less than {STALE_CLAIM_MINUTES} minutes ago and may "
    "be posting it right now; wait for that run to finish, and release it only if it never "
    "does"
)
_CLAIMED = (
    "publishing has already claimed this draft and will not re-read its status; wait until "
    'that run finishes before reopening it. If that run died without posting, "Release" '
    "puts the draft back in line first"
)


def block_reason(conn: sqlite3.Connection, draft_id: int, info) -> str:
    """Why step 3 will not let go of this draft, or "" when it will. The posts log is asked
    first and on its own: a schedule row can be missing, deleted or lagging while tweets
    exist, and a draft that reached X is never reopened whatever the schedule says. `info`
    is that draft's `store.publish_states` entry, or None when step 3 never saw it."""
    if publish_store.is_live(conn, draft_id):
        return _LIVE
    if info is None or info.reopenable:
        return ""
    if info.status == "claimed":
        return _CLAIMED
    return (
        f"publishing still holds this draft (it is {info.status}); it cannot be reopened "
        "until that is resolved"
    )


def forget(conn: sqlite3.Connection, draft_id: int) -> list[int]:
    """Let step 3 drop the draft's schedule row, so a saved publish order cannot come back
    with it on re-approval. Never touches a live or claimed row; empty before step 3's first
    run, when its tables do not exist."""
    return publish_store.forget(conn, [draft_id])


def stale_claim(info, now: datetime | None = None) -> bool:
    """Whether this draft's claim is old enough to be a dead run rather than a live one.
    Pure: `now` is a parameter. An unparsable or missing `claimed_at` is never stale, so a
    row the age cannot be judged from holds the draft instead of being released."""
    if info is None or info.status != publish_store.SCHED_CLAIMED or not info.claimed_at:
        return False
    try:
        claimed = datetime.fromisoformat(info.claimed_at)
    except ValueError:
        return False
    if claimed.tzinfo is None:
        claimed = claimed.replace(tzinfo=UTC)
    age = (now or datetime.now(UTC)) - claimed
    return age.total_seconds() >= STALE_CLAIM_MINUTES * 60


def release_reason(conn: sqlite3.Connection, draft_id: int, info, now=None) -> str:
    """Why step 3 will not put this draft back in line, or "" when it will. The posts log
    is asked first and on its own, as in `block_reason`: nothing that reached X is ever
    released. `info` is that draft's `store.publish_states` entry, or None when step 3
    never saw it."""
    if publish_store.is_live(conn, draft_id):
        return _LIVE_RELEASE
    if info is None or info.status == publish_store.SCHED_PENDING:
        return _NOT_HELD
    if info.status in RELEASABLE_STATES:
        return ""
    if info.status == publish_store.SCHED_CLAIMED:
        return "" if stale_claim(info, now) else _FRESH_CLAIM
    return (
        f"publishing still holds this draft (it is {info.status}); it cannot be released "
        "until that is resolved"
    )


def release(conn: sqlite3.Connection, draft_id: int, info, now=None) -> list[int]:
    """Drop the draft's dead schedule row so `fetch_approved` picks it up again. The draft
    itself is untouched: it stays approved, with its saved publishing order. Returns the
    released draft ids (empty when step 3's tables do not exist, or when the row slipped
    out from under the guard — a run claiming it between the check and here)."""
    if info is not None and info.status == publish_store.SCHED_CLAIMED:
        return publish_store.release_claimed(conn, [draft_id], now=now)
    return publish_store.release_failed(conn, [draft_id])
