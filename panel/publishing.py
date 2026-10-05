"""The approved page's publishing actions, behind the control panel only.

Manual posting (`posting: manual` in publish/config.yaml): `manual_post` gathers what the
copy-paste page shows for one approved draft (each post's final text, numbered and
re-checked exactly as run_publish.py would post it, the pictures anchored to it, and a
studio piece's facts to re-check on posting day, `approval_queue/store.py:recheck_lines`), and
`confirm_manual` logs it as posted through step 3's own `publish.store.record_manual` once
the human says it is on X. Nothing here talks to X.

`parse_order` is pure: the "Set schedule" form's `order_<draft id>` boxes become the
ordered list of draft ids (blank boxes drop out, ties keep the page's order). `save_order`
writes that order through step 3's own `publish.store.set_order`, touching only
`schedule.position` on unclaimed rows, never a draft or a post.

`add_head_link` is the studio performance page's "add the post's link" (wired into
studio/web.py by panel/app.py): a draft confirmed with "I posted it" but no link gets its
X id afterwards through step 3's own `publish.store.set_head_tweet`, which replaces only
post 1's `manual-` marker, so step 4 can fetch the numbers the studio learns from.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime

import run_publish
from approval_queue import store as queue_store
from publish import store as publish_store
from publish.scheduler import load_publish_config
from publish.thread import ThreadError

ORDER_PREFIX = "order_"


def parse_order(form: dict[str, list[str]]) -> list[int]:
    """`{"order_12": ["2"], "order_7": ["1"], "order_9": [""]}` -> `[7, 12]`."""
    ranked: list[tuple[int, int, int]] = []
    for seq, (key, values) in enumerate(form.items()):
        if not key.startswith(ORDER_PREFIX):
            continue
        raw = (values[-1] if values else "").strip()
        if not raw:
            continue
        try:
            draft_id = int(key[len(ORDER_PREFIX) :])
            position = int(raw)
        except ValueError as exc:
            raise ValueError(f"{key}: the order must be a whole number") from exc
        if position < 1:
            raise ValueError(f"{key}: the order starts at 1")
        ranked.append((position, seq, draft_id))
    ranked.sort()
    return [draft_id for _, _, draft_id in ranked]


def save_order(ordered_ids: list[int]) -> int:
    conn = publish_store.connect()
    try:
        return publish_store.set_order(conn, ordered_ids)
    finally:
        conn.close()


@dataclass
class ManualPost:
    """One post of the copy-paste page: its text and its pictures [(index, alt)]."""

    position: int
    text: str
    images: list[tuple[int, str]] = field(default_factory=list)


@dataclass
class ManualDraft:
    draft_id: int
    title: str
    shape: str
    posts: list[ManualPost]
    image_paths: list[str]  # by index, what /publishing/manual/{id}/image/{index} serves
    error: str | None = None  # the text fails a hard check: post nothing
    blocked: str | None = None  # the daily cap or the gap says wait (a warning only)
    status: str | None = None  # step 3's state when the draft is no longer waiting
    # Fast-moving facts to confirm before posting (a studio piece's recheck_before_posting):
    # the session wrote them for posting day, and this page is where posting happens.
    recheck: list[str] = field(default_factory=list)


def manual_post(draft_id: int, now: datetime | None = None) -> ManualDraft | None:
    """What to paste for one draft, or None when it is not an approved draft. A draft that
    is already claimed (posted, or a run has it) comes back with `status` set and no posts."""
    cfg = load_publish_config()
    conn = publish_store.connect()
    try:
        found = publish_store.fetch_approved(limit=1, conn=conn, draft_ids=[draft_id])
        if not found:
            row = publish_store.get_schedule(conn, draft_id)
            if row is None:
                return None
            return ManualDraft(draft_id, "", "", [], [], status=row["status"])
        approved = found[0]
        policy = run_publish.build_policy(conn, cfg, now or datetime.now(UTC))
        blocked = policy.blocked_reason(now or datetime.now(UTC))
    finally:
        conn.close()
    piece = queue_store.studio_piece_id(approved.item_id)
    # A studio draft has no feed item, so no title of its own: the queue names it this way.
    title = approved.title or (f"Studio piece {piece}" if piece is not None else "")
    recheck = queue_store.recheck_lines(approved.why_it_matters)
    try:
        _, texts = run_publish.texts_for(approved, cfg["thread_numbering"])
    except ThreadError as exc:
        return ManualDraft(draft_id, title, approved.shape, [], [], error=str(exc), recheck=recheck)
    paths: list[str] = []
    posts = [ManualPost(i, t) for i, t in enumerate(texts, 1)]
    for pos, pics in sorted(run_publish.images_for(approved, cfg).items()):
        target = posts[min(pos, len(posts)) - 1]
        for path, alt in pics:
            target.images.append((len(paths), alt))
            paths.append(path)
    return ManualDraft(
        draft_id, title, approved.shape, posts, paths, blocked=blocked, recheck=recheck
    )


def confirm_manual(draft_id: int, first_url: str = "") -> bool:
    """Log a hand-posted draft as posted. False when it is not waiting any more."""
    draft = manual_post(draft_id)
    if draft is None or draft.status or draft.error or not draft.posts:
        return False
    conn = publish_store.connect()
    try:
        return publish_store.record_manual(conn, draft_id, [p.text for p in draft.posts], first_url)
    finally:
        conn.close()


def add_head_link(draft_id: int, url: str) -> str:
    """Give a draft posted by hand without its link its X id. Raises ValueError with the
    reason it cannot."""
    conn = publish_store.connect()
    try:
        tid = publish_store.set_head_tweet(conn, draft_id, url)
    finally:
        conn.close()
    return f"post {tid} linked to draft {draft_id}: the next feedback snapshot fetches its numbers"
