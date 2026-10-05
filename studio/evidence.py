"""What X says, read from the database: each posted studio piece measured on its first
post, every posted head as the account's baseline, and from them the evidence block and
the lean a brief carries. The arithmetic is studio/learn.py's (pure); this module only
gathers the rows, through studio/store.py's read-only adapters and studio/ingest.py.

A piece's numbers come from step 4's snapshots of its first post or, when the account
has no X API read access or the post was confirmed without its link, from what the
editor typed in on the performance page (studio_manual_metrics).
"""

from __future__ import annotations

import random
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from studio import learn as L
from studio import store as S

POST_URL = "https://x.com/i/web/status/{}"


def parse_when(text: str | None) -> datetime | None:
    """An aware datetime from a stored ISO string, or None (blank, malformed or naive)."""
    try:
        dt = datetime.fromisoformat(str(text or "").replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else None


def _snapshots(raw: list[dict[str, Any]], *, manual: bool) -> list[dict[str, Any]]:
    out = []
    for snap in raw:
        when = parse_when(snap.get("captured_at"))
        if when is not None:
            out.append({"captured_at": when, "counts": snap["counts"], "manual": manual})
    return sorted(out, key=lambda s: s["captured_at"])


@dataclass
class Posted:
    """A posted studio piece as the performance page lists it, scored or not."""

    piece: S.Piece
    draft_id: int
    posted_at: datetime
    tweet_id: str
    url: str  # "" for a post confirmed by hand without its link
    measured: L.Measured | None = None  # None while it has no usable numbers


@dataclass
class Evidence:
    measured: list[L.Measured]  # posted studio pieces with usable numbers, oldest first
    heads: list[L.Head]  # every posted head with a value: the baseline material
    posted: list[Posted] = field(default_factory=list)  # every posted studio piece, newest first

    @property
    def scored(self) -> list[L.Measured]:
        return L.scored(self.measured)

    @property
    def waiting(self) -> list[Posted]:
        """Posted pieces with no usable numbers yet (younger than the horizon, or no
        snapshot at all)."""
        return [p for p in self.posted if p.measured is None]


def measure(conn: sqlite3.Connection, cfg: dict[str, Any]) -> Evidence:
    """Every posted head with a value at the horizon, and the studio pieces among them
    scored against the account's trailing median."""
    from approval_queue import store as queue_store
    from studio import ingest

    lcfg = cfg["learn"]
    kpi, horizon = str(lcfg["kpi"]), float(lcfg["horizon_hours"])
    pieces = S.list_pieces(conn, 1_000_000)
    by_id = {p.id: p for p in pieces}
    by_draft = {p.draft_id: p for p in pieces if p.draft_id is not None}
    typed = S.manual_metrics(conn)
    heads: list[L.Head] = []
    measured: list[L.Measured] = []
    posted: list[Posted] = []
    for head in S.fetch_posted_heads(conn):
        when = parse_when(head.posted_at)
        if when is None:
            continue
        piece_id = queue_store.studio_piece_id(head.item_id)
        piece = by_id.get(piece_id) if piece_id is not None else by_draft.get(head.draft_id)
        snaps = _snapshots(head.snapshots, manual=False)
        if piece is not None:
            snaps += _snapshots(typed.get(piece.id, []), manual=True)
        snap = L.pick_snapshot(snaps, when, horizon)
        value = L.kpi_value(snap["counts"], kpi) if snap is not None else None
        if value is not None:
            heads.append(L.Head(draft_id=head.draft_id, posted_at=when, value=value))
        if piece is None:
            continue
        row = Posted(
            piece=piece,
            draft_id=head.draft_id,
            posted_at=when,
            tweet_id=head.tweet_id,
            url=POST_URL.format(head.tweet_id) if head.measurable else "",
        )
        posted.append(row)
        if snap is None or value is None:
            continue
        row.measured = L.Measured(
            piece_id=piece.id,
            draft_id=head.draft_id,
            posted_at=when,
            value=value,
            metrics={k: int(v) for k, v in snap["counts"].items()},
            title=piece.label,
            opening=head.text[:400],
            angle=piece.angle,
            shape=piece.shape,
            hook_style=piece.hook_style,
            cards=ingest.draft_cards(conn, head.draft_id),
            source="manual" if snap.get("manual") else "x",
            url=row.url,
        )
        measured.append(row.measured)
    scored = L.score(
        measured,
        heads,
        baseline_days=float(lcfg["baseline_days"]),
        min_baseline_posts=int(lcfg["min_baseline_posts"]),
        smoothing=float(lcfg["smoothing"]),
    )
    posted.sort(key=lambda p: (p.posted_at, p.piece.id), reverse=True)
    return Evidence(measured=scored, heads=heads, posted=posted)


def block(evidence: Evidence, cfg: dict[str, Any]) -> str:
    """The "WHAT X SAYS" text for a session; "" while nothing is scored."""
    lcfg = cfg["learn"]
    return L.evidence_block(
        evidence.measured,
        kpi=str(lcfg["kpi"]),
        horizon_hours=float(lcfg["horizon_hours"]),
        baseline_days=float(lcfg["baseline_days"]),
    )


def lean_for(
    evidence: Evidence,
    cfg: dict[str, Any],
    *,
    angles: Sequence[str],
    shapes: Sequence[str],
    hooks: Sequence[str],
    seed: int,
) -> L.Lean | None:
    """The lean one piece is offered. Seeded by the piece, so every stage of the same piece
    sees the same lean while the evidence stands still."""
    lcfg = cfg["learn"]
    return L.lean(
        evidence.measured,
        angles=angles,
        shapes=shapes,
        hooks=hooks,
        rng=random.Random(seed),
        prior_sd=float(lcfg["prior_sd"]),
        default_sd=float(lcfg["post_sd"]),
        min_measured=int(lcfg["lean_min_measured"]),
    )


def edits(conn: sqlite3.Connection, limit: int = 12) -> list[L.Edit]:
    """The editor's hand edits of studio drafts, newest first, for the playbook rewrite."""
    return [
        L.Edit(piece_id=pid, before=before, after=after)
        for pid, before, after in S.fetch_studio_edits(conn, limit)
    ]
