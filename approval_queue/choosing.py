"""Step 9 with `jury: human` (swarm/config.yaml): the human A/B pick.

run_draft.py stores a story the swarm and the control both drafted as ONE draft row in
status 'choosing', holding one variant, with the other in drafts.choice_json and a coin
flip saying which is shown as A. The queue's /choose pages show both blind; the pick makes
the row 'pending' with the picked text (store.resolve_choice), records the winner on the
swarm run (swarm/store.py:record_human_pick, which evolve's credit reads), and only then
draws the picture, in the designer's Style, through the usual render-grade loop.

This module is the queue's one door to swarm/store.py. Pictures on the pick page are
quick previews (render_chart, no grading), deleted once the pick is made.
"""

from __future__ import annotations

import logging
import sqlite3

from approval_queue import images, store
from draft.chart import Chart, Style, render_chart
from draft.schema import Draft
from swarm import store as swarm_store

log = logging.getLogger(__name__)

SIDES = ("A", "B")


def render_previews(
    conn: sqlite3.Connection, draft_id: int, *, source_url: str = "", style: Style | None = None
) -> None:
    """Draw every chart of both variants to store.preview_file. Fail-soft: a side without
    a preview shows its chart spec as text. Tables are never drawn before verify."""
    choice = store.get_choice(conn, draft_id)
    if choice is None:
        return
    for label in SIDES:
        for k, visual in enumerate(choice.side(label).draft.visuals):
            if not isinstance(visual, Chart):
                continue
            try:
                drawn, logos = images._brand_chart(visual)
                render_chart(
                    drawn,
                    store.preview_file(draft_id, label, k),
                    source_url=source_url,
                    style=style,
                    logos=logos,
                )
            except Exception as exc:  # matplotlib missing, a bad spec: no preview, no harm
                log.warning("draft %d: preview %s%d not drawn: %s", draft_id, label, k, exc)


def preview_indexes(draft_id: int, label: str, draft: Draft) -> list[int]:
    """Indexes of this side's visuals that have a preview on disk."""
    return [
        k for k in range(len(draft.visuals)) if store.preview_file(draft_id, label, k).is_file()
    ]


def pick(
    conn: sqlite3.Connection,
    draft_id: int,
    label: str,
    *,
    source_url: str = "",
    cfg: dict | None = None,
) -> store.Variant:
    """The human picked side `label`. Returns the picked variant (its `role` says whether
    it was the swarm's or the control's). KeyError when the draft awaits no pick."""
    picked = store.resolve_choice(conn, draft_id, label)
    store.drop_previews(draft_id)
    run_id = swarm_store.run_for_draft(conn, draft_id)
    if run_id is not None:
        swarm_store.record_human_pick(conn, run_id, picked.role)
    stored = store.get_style(conn, draft_id)
    style = Style().apply(stored) if stored else None
    drawn = images.attach_chart(
        conn, draft_id, picked.draft.chart, source_url=source_url, cfg=cfg, style=style
    )
    if picked.draft.extra_visuals:
        drawn = (
            images.attach_extra_charts(
                conn,
                draft_id,
                picked.draft.extra_visuals,
                source_url=source_url,
                cfg=cfg,
                style=style,
            )
            or drawn
        )
    if drawn and run_id is not None and style is not None:
        swarm_store.mark_styled(conn, run_id)
    log.info("draft %d: human picked %s (%s)", draft_id, label, picked.role)
    return picked


def reject_both(
    conn: sqlite3.Connection, draft_id: int, note: str | None, category: str | None
) -> None:
    """Neither variant is worth posting: the draft is rejected (the row's variant is what
    the rejection records) and the run has no winner."""
    store.reject(conn, draft_id, note=note, category=category)
    store.drop_previews(draft_id)
    run_id = swarm_store.run_for_draft(conn, draft_id)
    if run_id is not None:
        swarm_store.record_human_pick(conn, run_id, None)
    log.info("draft %d: both variants rejected", draft_id)
