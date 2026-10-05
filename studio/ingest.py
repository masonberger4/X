"""A finished studio piece into the approval queue, through approval_queue/store.py's own
functions (the studio's one write outside its tables).

The piece becomes an ordinary pending draft: shape `long` with the studio's per-post
limit (one long post, or several posts that publish chains as replies, never numbered),
every card copied into the queue's image folder and anchored to its post. The draft
carries no claims to verify (the session ran its own cold fact-check), so step 2b leaves
it alone. Its item_id is `studio:<piece id>` (approval_queue/store.py:studio_item_id),
which is how the queue knows to send a revision back to the studio instead of the
drafter.
"""

from __future__ import annotations

import logging
import shutil
import sqlite3

from approval_queue import store as queue_store
from draft.schema import SHAPE_LONG, Draft
from studio import qa
from studio import store as S

log = logging.getLogger(__name__)


class IngestError(RuntimeError):
    """The piece cannot go into the queue (its draft is no longer pending)."""


def _why(piece: S.Piece, pf: qa.PieceFiles, report: qa.Report) -> str:
    lines = [pf.summary or piece.label]
    if pf.angle:
        lines.append(f"Angle: {pf.angle}" + (f" ({pf.angle_reason})" if pf.angle_reason else ""))
    if pf.recheck:
        lines.append("Re-check before posting: " + "; ".join(pf.recheck))
    if report.warnings:
        lines.append("For the editor: " + "; ".join(report.warnings))
    lines.append(f"Fact base, fact-check log and session: /studio/{piece.id}")
    return "\n".join(lines)


def build_draft(piece: S.Piece, report: qa.Report, max_chars: int) -> Draft:
    pf = report.piece
    if pf is None or not pf.posts:
        raise IngestError("the piece has no posts")
    cards = pf.cards
    return Draft(
        thread=list(pf.posts),
        suggested_visual="; ".join(
            f"card {i}: {c.alt[:160]}" for i, c in enumerate(cards, start=1)
        ),
        why_it_matters=_why(piece, pf, report),
        claims_to_verify=[],
        shape=SHAPE_LONG,
        anchors=[max(1, min(c.post, len(pf.posts))) for c in cards] or [1],
        max_chars=max_chars,
        wanted_visuals=len(cards),
    )


def _not_pending(existing: queue_store.DraftRow) -> str:
    return (
        f"draft {existing.id} is {existing.status}, not pending; reopen it in the queue "
        "before revising"
    )


def revise_blocker(conn: sqlite3.Connection, piece: S.Piece) -> str:
    """Why a revision of this piece could not replace its queue draft, or "" when it can:
    the rule to_queue applies at the end, asked before an hour-long revision starts."""
    existing = queue_store.find_by_item(conn, queue_store.studio_item_id(piece.id))
    if existing is None or existing.status == queue_store.STATUS_PENDING:
        return ""
    return _not_pending(existing)


def to_queue(
    conn: sqlite3.Connection, piece: S.Piece, report: qa.Report, *, max_chars: int = 25000
) -> int:
    """Insert the piece as a pending draft, or replace the text and cards of its pending
    draft after a revision. Returns the draft id."""
    draft = build_draft(piece, report, max_chars)
    item_id = queue_store.studio_item_id(piece.id)
    model = f"{piece.model} (studio)"
    existing = queue_store.find_by_item(conn, item_id)
    if existing is None:
        draft_id = queue_store.insert_draft(
            conn, item_id=item_id, model=model, draft=draft, cluster_id=piece.cluster_id
        )
    else:
        if existing.status != queue_store.STATUS_PENDING:
            raise IngestError(_not_pending(existing))
        draft_id = existing.id
        # The editor's words, as the queue's own revise logs them. session.revise keeps
        # them in meta: the request's note is cleared when the revision starts.
        note = str(piece.meta.get("revise_note") or "") or piece.request_note
        queue_store.revise(
            conn,
            draft_id,
            draft=draft,
            model=model,
            note=note or "revised in the studio",
        )
    _attach_cards(conn, draft_id, report)
    return draft_id


def _attach_cards(conn: sqlite3.Connection, draft_id: int, report: qa.Report) -> None:
    pf = report.piece
    cards = pf.cards if pf else []
    for k, card in enumerate(cards):
        dst = queue_store.image_file(draft_id, k)
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(card.png, dst)  # a copy: dropping a picture in the queue deletes it
        queue_store.set_image(conn, draft_id, dst, card.alt, index=k)
    # A revision with fewer cards leaves no stale picture behind.
    k = len(cards)
    while True:
        stale = queue_store.image_file(draft_id, k)
        if not stale.exists():
            break
        try:
            stale.unlink()
        except OSError:
            log.warning("could not remove %s", stale)
        k += 1
