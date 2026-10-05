"""A finished studio piece into the approval queue, through approval_queue/store.py's own
functions (the studio's one write outside its tables).

The piece becomes an ordinary pending draft: shape `long` with the studio's per-post
limit (one long post, or several posts that publish chains as replies, never numbered),
every card copied into the queue's image folder and anchored to its post. The draft
carries no claims to verify (the session ran its own cold fact-check), so step 2b leaves
it alone. Its item_id is `studio:<piece id>` (approval_queue/store.py:studio_item_id),
which is how the queue knows to send a revision back to the studio instead of the
drafter.

The editor may change the draft in the queue after the studio put it there: the text by
hand, a card dropped. The session never sees those changes, so every ingest records what
it put in the queue (`queued` in the piece's meta) and `hand_edits` compares the draft with
that record. A revision starts from the editor's version (studio/session.py writes it back
into the session's files and says so in the prompt), and `to_queue` refuses to replace a
draft whose changes the session was never given: a hand edit is never reverted silently.
"""

from __future__ import annotations

import logging
import shutil
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from approval_queue import publishing
from approval_queue import store as queue_store
from draft.schema import SHAPE_LONG, Draft
from studio import qa
from studio import store as S

log = logging.getLogger(__name__)


class IngestError(RuntimeError):
    """The piece cannot go into the queue (its draft was approved or posted meanwhile)."""


def _target_line(entry: dict[str, Any]) -> str:
    """ "H.C. Wainwright $20 of 2026-09-30" from a piece.json price_targets entry."""
    who = " ".join(" ".join(str(entry.get(k) or "").split()) for k in ("firm", "target")).strip()
    date = " ".join(str(entry.get("date") or "").split())
    return f"{who} of {date}" if who and date else who


def _why(piece: S.Piece, pf: qa.PieceFiles, report: qa.Report) -> str:
    lines = [pf.summary or piece.label]
    if pf.angle:
        lines.append(f"Angle: {pf.angle}" + (f" ({pf.angle_reason})" if pf.angle_reason else ""))
    # One line per fact, so the copy-paste page lists them on posting day
    # (approval_queue/store.py:recheck_lines reads them back).
    lines += [queue_store.RECHECK_PREFIX + " ".join(r.split()) for r in pf.recheck]
    # Analysts move targets often: each one cited is re-checked on posting day too.
    targets = [line for line in map(_target_line, pf.price_targets) if line]
    if targets:
        lines.append(
            queue_store.RECHECK_PREFIX
            + "the analyst targets cited are still each firm's latest ("
            + "; ".join(targets)
            + ")"
        )
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


# A revision replaces a pending draft, and brings a rejected one back to pending (the
# editor asked for it after rejecting). An approved or posted draft waits for a reopen in
# the queue, where the publishing checks run.
_REVISABLE = (queue_store.STATUS_PENDING, queue_store.STATUS_REJECTED)


def _not_revisable(conn: sqlite3.Connection, existing: queue_store.DraftRow) -> str:
    """Why a revision could not replace this draft, or "". A rejected draft comes back to
    pending, so step 3 is asked the queue's own Reopen question first: nothing of it on X
    (a rejected draft may have been posted before it was rejected) and no publish run
    holding it."""
    if existing.status not in _REVISABLE:
        return (
            f"draft {existing.id} is {existing.status}, not pending; reopen it in the queue "
            "before revising"
        )
    if publishing.is_live(conn, existing.id):
        return (
            f"draft {existing.id} is already live on X, so a revision of it could never be "
            "posted; start a new piece instead"
        )
    info = queue_store.publish_states(conn, [existing.id]).get(existing.id)
    held = publishing.block_reason(conn, existing.id, info)
    return f"draft {existing.id} cannot come back for a revision: {held}" if held else ""


def revise_blocker(conn: sqlite3.Connection, piece: S.Piece) -> str:
    """Why a revision of this piece could not replace its queue draft, or "" when it can:
    the rule to_queue applies at the end, asked before an hour-long revision starts (by the
    studio page, the runner and session.revise alike)."""
    existing = queue_store.find_by_item(conn, queue_store.studio_item_id(piece.id))
    return "" if existing is None else _not_revisable(conn, existing)


def withdraw(conn: sqlite3.Connection, piece: S.Piece) -> int | None:
    """A discarded piece leaves nothing to approve: its draft is rejected when it is still
    pending. Returns that draft's id, or None when there was nothing to withdraw."""
    existing = queue_store.find_by_item(conn, queue_store.studio_item_id(piece.id))
    if existing is None or existing.status != queue_store.STATUS_PENDING:
        return None
    queue_store.reject(conn, existing.id, note="discarded in the studio")
    return existing.id


# --- the editor's changes in the queue ---------------------------------------------------


@dataclass
class HandEdits:
    """What the editor changed on the piece's queue draft since the studio last put it
    there. `posts` is the queue's text when the editor changed it by hand (the draft's
    thread: an edit saves it there, and it is what publish posts), `dropped` the recorded
    cards no longer attached ({"file", "alt", "post"} each, `file` as piece.json names it).
    False when nothing changed."""

    draft_id: int
    posts: list[str] | None = None
    dropped: list[dict[str, Any]] = field(default_factory=list)

    def __bool__(self) -> bool:
        return self.posts is not None or bool(self.dropped)

    def key(self) -> dict[str, Any]:
        """What session.revise records once these changes are in the session's files
        (`hand_edit` in the piece's meta), so a resumed revision does not write them again."""
        return {"posts": self.posts, "dropped": [str(c.get("file", "")) for c in self.dropped]}


def _card_file(workspace: Path, html: Path) -> str:
    """A card's file as piece.json names it: relative to the piece's folder."""
    try:
        return html.resolve().relative_to(workspace.resolve()).as_posix()
    except (OSError, ValueError):
        return html.name


def _record(draft_id: int, workspace: Path, pf: qa.PieceFiles) -> dict[str, Any]:
    """What an ingest puts in the queue: the posts, and each card with the post it is on
    (clamped as build_draft clamps it)."""
    return {
        "draft_id": draft_id,
        "posts": list(pf.posts),
        "cards": [
            {
                "file": _card_file(workspace, c.html),
                "alt": c.alt,
                "post": max(1, min(c.post, len(pf.posts))),
            }
            for c in pf.cards
        ],
    }


def _dropped(cards: list[dict[str, Any]], images: list[dict]) -> list[dict[str, Any]]:
    """The recorded cards the draft no longer carries. Dropping a picture in the queue keeps
    the others in order with their alt text and post, so what is left is a subsequence of
    the record, matched in order."""
    left = [(str(im.get("alt") or ""), int(im.get("anchor") or 1)) for im in images]
    gone, i = [], 0
    for card in cards:
        if i < len(left) and left[i] == (str(card.get("alt") or ""), int(card.get("post") or 1)):
            i += 1
        else:
            gone.append(card)
    return gone


def hand_edits(conn: sqlite3.Connection, piece: S.Piece) -> HandEdits | None:
    """The editor's changes to the piece's queue draft since the studio's last ingest, or
    None (no draft, or nothing changed, or nothing to compare with).

    A piece queued before ingests were recorded has no record. While it is ready its files
    are exactly what it last put in the queue, so they become its record, stored at once:
    a revision is about to change them, and comparing the queue with the revised files
    later would read the session's own work as the editor's."""
    row = queue_store.find_by_item(conn, queue_store.studio_item_id(piece.id))
    if row is None:
        return None
    record = piece.meta.get("queued")
    if not isinstance(record, dict) or record.get("draft_id") != row.id:
        if piece.stage != S.STAGE_READY:
            return None
        pf, _, _ = qa.read_piece(Path(piece.workspace))
        if pf is None or not pf.posts:
            return None
        record = _record(row.id, Path(piece.workspace), pf)
        S.update_piece(conn, piece.id, meta={"queued": record})
    posts = record.get("posts")
    cards = record.get("cards")
    edits = HandEdits(draft_id=row.id)
    if isinstance(posts, list) and list(row.draft.thread) != posts:
        edits.posts = list(row.draft.thread)
    if isinstance(cards, list):
        edits.dropped = _dropped([c for c in cards if isinstance(c, dict)], row.images)
    return edits if edits else None


# How a piece's draft stands, as the next pieces are told (one not on X was never published).
_WHERE = {
    queue_store.STATUS_PENDING: "waiting in the approval queue, not posted",
    queue_store.STATUS_APPROVED: "approved, not posted yet",
    queue_store.STATUS_REJECTED: "rejected by the editor, never posted",
}


def queued_text(conn: sqlite3.Connection, piece: S.Piece) -> tuple[list[str], str]:
    """A piece's posts as the approval queue holds them now (the editor's changes included)
    and where its draft stands, for the pieces after it to read (studio/prompt.py's
    EARLIER_FILE): a session cannot open another piece's folder, and the account's earlier
    words are the bar a scorecard grades against. ([], "") for a piece with no draft."""
    row = queue_store.find_by_item(conn, queue_store.studio_item_id(piece.id))
    if row is None or not row.draft.thread:
        return [], ""
    if publishing.is_live(conn, row.id):
        return list(row.draft.thread), "posted on X"
    return list(row.draft.thread), _WHERE.get(row.status, f"{row.status}, not posted")


def unsynced(piece: S.Piece, edits: HandEdits | None) -> bool:
    """Changes the session was never given: a revision has to start from them first."""
    return bool(edits) and edits.key() != _synced(piece)


def _synced(piece: S.Piece) -> dict[str, Any] | None:
    mark = piece.meta.get("hand_edit")
    if not isinstance(mark, dict):
        return None
    return {"posts": mark.get("posts"), "dropped": mark.get("dropped") or []}


def to_queue(
    conn: sqlite3.Connection, piece: S.Piece, report: qa.Report, *, max_chars: int = 25000
) -> int:
    """Insert the piece as a pending draft, or replace the text and cards of its pending
    draft after a revision. Returns the draft id, and records what went in (`queued` in the
    piece's meta) for the next `hand_edits`."""
    draft = build_draft(piece, report, max_chars)
    item_id = queue_store.studio_item_id(piece.id)
    model = f"{piece.model} (studio)"
    existing = queue_store.find_by_item(conn, item_id)
    if existing is None:
        draft_id = queue_store.insert_draft(
            conn, item_id=item_id, model=model, draft=draft, cluster_id=piece.cluster_id
        )
    else:
        blocked = _not_revisable(conn, existing)
        if blocked:
            raise IngestError(blocked)
        if unsynced(piece, hand_edits(conn, piece)):
            # The editor changed the draft in the queue and the session never saw it:
            # replacing it now would put the old text (or a dropped card) back.
            raise IngestError(
                f"draft {existing.id} was changed in the queue since the studio last put it "
                "there; Revise (or Resume) the piece so the session starts from those changes"
            )
        draft_id = existing.id
        if existing.status == queue_store.STATUS_REJECTED:
            queue_store.reopen(conn, draft_id, note="revised in the studio after it was rejected")
            # As the queue's own Reopen does: a publishing order saved while the draft was
            # approved must not come back with it on re-approval.
            publishing.forget(conn, draft_id)
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
    assert report.piece is not None  # build_draft refused a piece without one
    # The record says what the queue holds, from the moment the text is in: a card that
    # cannot be attached (a full disk, a picture another program holds open) fails the
    # piece after its new text replaced the old, and a record of the last ingest would then
    # read the session's own text and cards as the editor's hand edits on the next Resume.
    record = _record(draft_id, Path(piece.workspace), report.piece)
    _remember(conn, piece, record, attached=0)
    try:
        _attach_cards(conn, draft_id, report)
    finally:
        row = queue_store.get_draft(conn, draft_id)
        _remember(conn, piece, record, attached=len(row.images) if row else 0)
    return draft_id


def _remember(
    conn: sqlite3.Connection, piece: S.Piece, record: dict[str, Any], *, attached: int
) -> None:
    """Store an ingest's record with the cards that are attached so far: they are
    attached in order, so those are the first `attached` of the record's. A Resume then
    finds no hand edit and polishes again, which attaches the rest."""
    kept = {**record, "cards": record["cards"][:attached]}
    S.update_piece(conn, piece.id, meta={"queued": kept, "hand_edit": None})


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


def draft_cards(conn: sqlite3.Connection, draft_id: int) -> int:
    """How many pictures a piece's queue draft carries now (what went out with it)."""
    row = queue_store.get_draft(conn, draft_id)
    return len(row.images) if row is not None else 0
