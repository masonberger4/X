"""The radar's daily scan, and the catalysts each piece's research finds. The pure parts
(the prompt, reading the answer, the calendar's dates) are studio/radar.py's.

`call_scanner` is the scan's one model call: claude_cli.run_claude with WebSearch and
WebFetch only (no file tools), on the radar's model and effort (blank = the writer's),
with its own time limit. `run_scan` records the scan, asks, checks the answer and stores
its topics and catalysts; a failed call or an unusable answer is recorded as a failed scan
and changes nothing else. `harvest` reads a piece's research.json once its research is
done: the radar topic it chose, and the dated catalysts it found, for the calendar.
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

from studio import radar
from studio import store as S

log = logging.getLogger(__name__)

SCAN_TOOLS = ("WebSearch", "WebFetch")
RECENT_SHOWN = 15  # the account's recent pieces the scan is told about
KNOWN_SHOWN = 80  # catalysts already on the calendar the scan is told about


def call_scanner(
    system: str,
    user: str,
    *,
    model: str,
    effort: str,
    root_cfg: dict[str, Any],
    timeout: float,
) -> str:
    """The scan's one CLI call: web search and fetch only, and its own time limit in place
    of claude_code.timeout_seconds (a scan at max effort reads for many minutes)."""
    import claude_cli

    cli = {**(root_cfg.get("claude_code") or {}), "timeout_seconds": timeout}
    return claude_cli.run_claude(
        user,
        system=system,
        model=model,
        cfg={**root_cfg, "claude_code": cli},
        effort=effort or None,
        tools=list(SCAN_TOOLS),
    )


def scan_due(last: S.ScanRow | None, now: datetime, every_hours: float) -> bool:
    """A scan when none has finished yet, or the last finished `every_hours` ago."""
    if last is None:
        return True
    try:
        when = datetime.fromisoformat(last.finished_at or last.started_at)
    except ValueError:
        return True
    return now - when >= timedelta(hours=float(every_hours))


def calendar_window(rcfg: Mapping[str, Any], today: date) -> tuple[date, date]:
    """The span the calendar keeps: `past_days` back (for reaction pieces) to
    `calendar_days` ahead."""
    return (
        today - timedelta(days=int(rcfg["past_days"])),
        today + timedelta(days=int(rcfg["calendar_days"])),
    )


def known_catalysts(conn: sqlite3.Connection, rcfg: Mapping[str, Any], today: date) -> list[Any]:
    first, last = calendar_window(rcfg, today)
    return [
        row.to_catalyst() for row in S.catalysts_between(conn, first.isoformat(), last.isoformat())
    ]


def prompt_for(
    conn: sqlite3.Connection,
    cfg: dict[str, Any],
    *,
    today: date,
    tzname: str,
    angles: Mapping[str, str],
    feed: Sequence[str],
) -> tuple[str, str]:
    from timeutil import fmt_date

    rcfg = cfg["radar"]
    recent = [f"{fmt_date(p.created_at)} {p.label}" for p in S.recent_pieces(conn, RECENT_SHOWN)]
    return radar.scan_prompt(
        today=today.isoformat(),
        timezone=tzname,
        angles=angles,
        recent=recent,
        feed=feed,
        known=known_catalysts(conn, rcfg, today)[:KNOWN_SHOWN],
        max_topics=int(rcfg["max_topics"]),
    )


@dataclass(frozen=True)
class ScanOutcome:
    ok: bool
    message: str
    scan_id: int | None = None


def run_scan(
    conn: sqlite3.Connection,
    cfg: dict[str, Any],
    *,
    today: date,
    tzname: str,
    angles: Mapping[str, str],
    feed: Sequence[str],
    root_cfg: dict[str, Any],
    call: Callable[..., str] | None = None,
) -> ScanOutcome:
    """One scan, recorded in studio_scans whatever happens. Never raises for a failed call
    or an unusable answer: the scan is marked failed with the reason."""
    import claude_cli

    rcfg = cfg["radar"]
    system, user = prompt_for(conn, cfg, today=today, tzname=tzname, angles=angles, feed=feed)
    scan_id = S.start_scan(conn, model=str(rcfg["model"]), effort=str(rcfg["effort"]))
    timeout = float(rcfg.get("timeout_minutes") or 0) * 60 or 2400.0
    try:
        reply = (call or call_scanner)(
            system,
            user,
            model=str(rcfg["model"]),
            effort=str(rcfg["effort"]),
            root_cfg=root_cfg,
            timeout=timeout,
        )
    except claude_cli.ClaudeCliError as exc:
        why = f"the scan call failed: {exc}"
        S.finish_scan(conn, scan_id, status=S.SCAN_FAILED, detail=why)
        return ScanOutcome(False, f"scan {scan_id}: {why}", scan_id)
    try:
        result = radar.parse_scan(
            reply,
            angles=angles,
            today=today,
            max_topics=int(rcfg["max_topics"]),
            max_catalysts=int(rcfg["max_catalysts"]),
            past_days=int(rcfg["past_days"]),
            ahead_days=int(rcfg["calendar_days"]),
        )
    except radar.ScanRejected as exc:
        why = f"the scan's answer was not used: {exc}"
        S.finish_scan(conn, scan_id, status=S.SCAN_FAILED, detail=why)
        return ScanOutcome(False, f"scan {scan_id}: {why}", scan_id)
    S.add_radar_topics(conn, scan_id, result.topics)
    added, again = S.upsert_catalysts(conn, result.catalysts, origin=f"scan:{scan_id}")
    left_out = "; ".join(result.dropped[:20])
    S.finish_scan(
        conn,
        scan_id,
        status=S.SCAN_DONE,
        summary=result.summary,
        topics=len(result.topics),
        catalysts=added,
        detail=f"left out: {left_out}" if left_out else "",
    )
    message = (
        f"scan {scan_id}: {len(result.topics)} topic(s), {added} new catalyst(s), "
        f"{again} seen again"
    )
    return ScanOutcome(True, message + (f"; left out: {left_out}" if left_out else ""), scan_id)


def harvest(
    conn: sqlite3.Connection,
    cfg: dict[str, Any],
    piece: S.Piece,
    info: Mapping[str, Any],
    *,
    today: date,
) -> str:
    """What a piece's research.json gives the radar: the radar topic it chose (now used)
    and the dated catalysts it found (onto the calendar). Returns a line for the log."""
    rcfg = cfg["radar"]
    notes = []
    rid = info.get("radar_topic")
    # A radar number; JSON true is an int to Python and would name topic 1.
    if isinstance(rid, int) and not isinstance(rid, bool):
        topic = S.get_radar_topic(conn, rid)
        if topic is not None and topic.status in (S.RADAR_NEW, S.RADAR_QUEUED):
            S.set_radar_topic(conn, rid, status=S.RADAR_USED, piece_id=piece.id)
            notes.append(f"radar topic {rid} used")
    raw = info.get("catalysts")
    found = []
    for item in raw[: int(rcfg["max_catalysts"])] if isinstance(raw, list) else []:
        catalyst, _ = radar.read_catalyst(
            item,
            today=today,
            past_days=int(rcfg["past_days"]),
            ahead_days=int(rcfg["calendar_days"]),
        )
        if catalyst is not None:
            found.append(catalyst)
    if found:
        added, again = S.upsert_catalysts(conn, found, origin=f"piece:{piece.id}")
        notes.append(f"{added} new catalyst(s), {again} seen again")
    return "; ".join(notes)
