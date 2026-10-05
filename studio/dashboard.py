"""The performance page's view (/studio/performance), pure: no DB, no network, no clock.

studio/web.py gathers the rows (studio/evidence.py:measure, the playbook versions) and
hands them in with `now`; this module turns them into what the template shows: each
posted piece with its numbers, what each angle, shape, hook style and card count has done
and how often the lean suggests it, where the learning loop stands, and the playbook's
history with what each version changed.
"""

from __future__ import annotations

import difflib
import random
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from feedback.models import METRICS
from studio import learn as L
from studio import store as S
from studio.angles import HOOK_STYLES, SHAPES
from studio.evidence import Evidence, Posted, parse_when

SHARE_DRAWS = 4000
SHARE_SEED = 0  # the page shows the same shares on every load while the evidence stands


@dataclass
class PieceRow:
    piece_id: int
    label: str
    posted_at: datetime
    draft_id: int
    angle: str
    shape: str
    hook_style: str
    cards: int | None  # None while unmeasured (the page does not look it up)
    lean: dict[str, str]  # what the brief suggested, {} when none
    url: str
    needs_link: bool  # posted by hand without its link: step 4 cannot fetch numbers
    counts: dict[str, int] = field(default_factory=dict)
    value: float | None = None
    baseline: float | None = None
    relative: float | None = None
    source: str = ""  # "x" (step 4) or "manual" (typed in), "" while unmeasured
    status: str = ""  # why there is no score yet

    @property
    def followed(self) -> list[str]:
        """The parts of the lean the piece went with."""
        return [k for k, v in self.lean.items() if v and getattr(self, k, None) == v]


@dataclass
class ArmRow:
    value: str
    n: int
    times: float | None  # the typical piece with this value, as a multiple of the median
    share: float | None  # how often the lean suggests it; None for an arm it never draws


@dataclass
class ArmTable:
    arm: str
    label: str
    rows: list[ArmRow]


@dataclass
class VersionRow:
    version: S.PlaybookVersion
    diff: str
    current: bool
    status: str  # for a proposal: waiting, applied as version N, or overtaken
    open: bool


def _status(p: Posted, horizon_hours: float, now: datetime) -> str:
    if p.measured is not None:
        if p.measured.relative is None:
            return "measured; too few posts before it to compare with yet"
        return ""
    if not p.url:
        return "posted by hand without its link: add the link or type the numbers in"
    due = p.posted_at + timedelta(hours=horizon_hours)
    if now < due:
        return f"compared at {horizon_hours:g} hours: waiting until then"
    return "no snapshot yet: the feedback step takes them (X API read access), or type them in"


def piece_rows(ev: Evidence, cfg: dict[str, Any], now: datetime) -> list[PieceRow]:
    horizon = float(cfg["learn"]["horizon_hours"])
    out = []
    for p in ev.posted:
        m = p.measured
        lean = p.piece.meta.get("lean")
        out.append(
            PieceRow(
                piece_id=p.piece.id,
                label=p.piece.label,
                posted_at=p.posted_at,
                draft_id=p.draft_id,
                angle=p.piece.angle,
                shape=p.piece.shape,
                hook_style=p.piece.hook_style,
                cards=m.cards if m else None,
                lean={k: str(v) for k, v in lean.items() if v} if isinstance(lean, dict) else {},
                url=p.url,
                needs_link=not p.url,
                counts={k: int(m.metrics.get(k) or 0) for k in METRICS} if m else {},
                value=m.value if m else None,
                baseline=m.baseline if m else None,
                relative=m.relative if m else None,
                source=m.source if m else "",
                status=_status(p, horizon, now),
            )
        )
    return out


def arm_tables(ev: Evidence, cfg: dict[str, Any], *, angles: Sequence[str]) -> list[ArmTable]:
    """Per arm, every value the account can use (untried ones too) with its measured
    pieces, its multiple of the median and the share of leans it gets."""
    lcfg = cfg["learn"]
    offer = {"angle": list(angles), "shape": list(SHAPES), "hook_style": list(HOOK_STYLES)}
    enough = len(ev.scored) >= max(1, int(lcfg["lean_min_measured"]))
    tables = []
    for arm in L.ARMS:
        stats = {s.value: s for s in L.arm_stats(ev.measured, arm)}
        candidates = offer.get(arm, [])
        shares = (
            L.lean_shares(
                ev.measured,
                arm,
                candidates,
                rng=random.Random(SHARE_SEED),
                prior_sd=float(lcfg["prior_sd"]),
                default_sd=float(lcfg["post_sd"]),
                draws=SHARE_DRAWS,
            )
            if enough and candidates
            else {}
        )
        values = list(dict.fromkeys([*stats, *candidates]))
        rows = [
            ArmRow(
                value=v,
                n=stats[v].n if v in stats else 0,
                times=stats[v].times if v in stats else None,
                share=shares.get(v) if shares else None,
            )
            for v in values
        ]
        rows.sort(key=lambda r: (-(r.share or 0), -(r.times or 0), -r.n, r.value))
        tables.append(ArmTable(arm=arm, label=L.ARM_LABELS[arm], rows=rows))
    return tables


def _base_for(v: S.PlaybookVersion, ordered: Sequence[S.PlaybookVersion]) -> str:
    """The text the sessions read just before `v` was written: the newest applied version
    before it (a proposal is never one; its applied copy is)."""
    base = ""
    for other in ordered:
        if other.id >= v.id:
            break
        if other.applied and other.source != S.PLAYBOOK_PROPOSAL:
            base = other.text
    return base


def diff(before: str, after: str) -> str:
    lines = difflib.unified_diff(
        before.splitlines(), after.splitlines(), "before", "after", lineterm="", n=1
    )
    return "\n".join(list(lines)[2:])  # without the file header lines


def version_rows(
    versions: Sequence[S.PlaybookVersion], open_proposal: S.PlaybookVersion | None
) -> list[VersionRow]:
    """Newest first, each with what it changed against the playbook in use before it."""
    ordered = sorted(versions, key=lambda v: v.id)
    current = next(
        (v.id for v in reversed(ordered) if v.applied and v.source != S.PLAYBOOK_PROPOSAL),
        None,
    )
    applied_as = {
        v.based_on: v.id for v in ordered if v.source == S.PLAYBOOK_LEARNED and v.based_on
    }
    rows = []
    for v in reversed(ordered):
        status = ""
        if v.source == S.PLAYBOOK_PROPOSAL:
            if open_proposal is not None and v.id == open_proposal.id:
                status = "waiting for you to apply it"
            elif v.id in applied_as:
                status = f"applied as version {applied_as[v.id]}"
            else:
                status = "overtaken by a later version"
        before = _base_for(v, ordered)
        rows.append(
            VersionRow(
                version=v,
                diff=diff(before, v.text) if before else "",
                current=v.id == current,
                status=status,
                open=bool(open_proposal is not None and v.id == open_proposal.id),
            )
        )
    return rows


@dataclass
class LearnState:
    mode: str
    last: S.PlaybookVersion | None
    new_pieces: int
    min_new: int
    min_hours: float
    due: bool
    next_after: datetime | None  # the earliest the next rewrite may run (time permitting)


def learn_state(
    ev: Evidence, cfg: dict[str, Any], last: S.PlaybookVersion | None, now: datetime
) -> LearnState:
    lcfg = cfg["learn"]
    learned_from = last.pieces if last else ()
    last_at = parse_when(last.created_at) if last else None
    min_hours = float(lcfg["rewrite_min_hours"])
    return LearnState(
        mode=str(lcfg["playbook"]),
        last=last,
        new_pieces=len(L.unseen(ev.measured, learned_from)),
        min_new=int(lcfg["rewrite_min_new"]),
        min_hours=min_hours,
        due=L.rewrite_due(
            ev.measured,
            learned_from=learned_from,
            last_rewrite_at=last_at,
            now=now,
            min_new=int(lcfg["rewrite_min_new"]),
            min_hours=min_hours,
        ),
        next_after=last_at + timedelta(hours=min_hours) if last_at else None,
    )
