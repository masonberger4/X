"""The approved page's two publishing actions, behind the control panel only.

`parse_order` is pure: the "Set schedule" form's `order_<draft id>` boxes become the
ordered list of draft ids (blank boxes drop out, ties keep the page's order). `save_order`
writes that order through step 3's own `publish.store.set_order`: the second and last row
the panel writes outside its own pages (the feed page's rating is the first), and it
touches only `schedule.position` on unclaimed rows, never a draft or a post.
"""

from __future__ import annotations

from publish import store as publish_store

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
