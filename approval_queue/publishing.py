"""What the queue needs from step 3 when a human takes a draft back.

Reopening an approved draft is the one place step 2 writes into step 3's tables, and it
writes them only through `publish/store.py:forget`, which refuses anything live. Everything
else the queue knows about publishing it reads through `store.publish_states`. Keeping both
calls here rather than in a route mirrors `panel/publishing.py`, so the carve-out is stated
in one place instead of being spelled out at the call site.
"""

from __future__ import annotations

import sqlite3

from publish import store as publish_store

_LIVE = (
    "this draft is already live on X and cannot be reopened; reject it instead if it "
    "should not run again"
)
_CLAIMED = (
    "publishing has already claimed this draft and will not re-read its status; wait until "
    "that run finishes before reopening it"
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
