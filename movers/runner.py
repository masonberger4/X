"""One market-movers run (run_movers.py): screen the universe's prices, check the new
movers for a story in one call, put the ones worth a piece on the studio's radar and queue
the best for the studio.

The studio is the only writer: a mover becomes a radar topic (studio_radar_topics, with
scan_id 0, which no scan uses) and, for the best `auto_queue` of them, a queued studio
topic with no angle named, so the studio's session picks the angle and writes in the voice
the app gives it, as for any other topic. Writes to studio tables go through
studio/store.py's own functions.
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from movers import check as CK
from movers import prices as PR
from movers import screen as SC
from movers import store as M
from movers.settings import load_movers_config

log = logging.getLogger(__name__)

LOCK_NAME = ".movers.lock"
RADAR_SCAN_ID = 0  # the scan_id of a radar topic market movers put there


def _root_config() -> dict[str, Any]:
    from config import load_config

    return load_config()


def _db_conn() -> sqlite3.Connection:
    from approval_queue import store as queue_store
    from studio import store as S

    conn = queue_store.connect()
    S.ensure_tables(conn)
    M.ensure_tables(conn)
    return conn


def _data_folder() -> Path:
    import os

    from approval_queue import store as queue_store

    return Path(os.path.abspath(queue_store.db_path())).parent


def due(last: M.RunRow | None, now: datetime, cfg: dict[str, Any]) -> tuple[bool, str]:
    """(due, why not): not before `not_before` New York time, and `every_hours` after the
    last screen that finished."""
    local = now.astimezone(ZoneInfo(SC.MARKET_TZ))
    earliest = SC.market_time(cfg["not_before"])
    if local.time() < earliest:
        return False, f"it is before {earliest:%H:%M} in New York"
    if last is not None:
        try:
            when = datetime.fromisoformat(last.started_at)
        except ValueError:
            return True, ""
        if now - when < timedelta(hours=float(cfg["every_hours"])):
            return False, f"the last screen ({last.started_at}) is under {cfg['every_hours']}h old"
    return True, ""


def screen_all(
    names: dict[str, str],
    cfg: dict[str, Any],
    *,
    now: datetime,
    fetch: PR.Fetch | None = None,
    sleep: Callable[[float], None] | None = None,
) -> tuple[list[SC.Mover], list[str], SC.Mover | None]:
    """(movers that pass the screen, tickers whose prices could not be read, the
    benchmark measured). Fails soft per ticker."""
    kw: dict[str, Any] = {"fetch": fetch}
    if sleep is not None:
        kw["sleep"] = sleep
    movers, failed = [], []
    for ticker, company in names.items():
        try:
            daily, intraday = PR.charts(ticker, cfg["prices"], **kw)
            mover = SC.measure(ticker, daily, intraday, now=now, company=company)
        except Exception as exc:  # noqa: BLE001 - one ticker never stops the screen
            log.warning("%s: no prices (%s)", ticker, exc)
            failed.append(ticker)
            continue
        if mover is None:
            log.debug("%s: fewer than two completed sessions", ticker)
            continue
        passed = SC.screen(mover, cfg)
        if passed is not None:
            movers.append(passed)
    bench = None
    symbol = str(cfg.get("benchmark") or "").upper()
    if symbol:
        try:
            daily, intraday = PR.charts(symbol, cfg["prices"], **kw)
            bench = SC.measure(symbol, daily, intraday, now=now)
        except Exception as exc:  # noqa: BLE001
            log.warning("benchmark %s: no prices (%s)", symbol, exc)
    movers.sort(key=lambda m: -abs(m.biggest.pct))
    return movers, failed, bench


def _new_only(conn: sqlite3.Connection, movers: list[SC.Mover]) -> list[SC.Mover]:
    """Each mover with only the moves no earlier screen recorded; those left with none
    are dropped."""
    out = []
    for m in movers:
        fresh = [x for x in m.flagged if not M.seen(conn, m.ticker, x.session, x.day.isoformat())]
        if fresh:
            m.flagged = fresh
            out.append(m)
    return out


def _record(
    conn: sqlite3.Connection,
    run_id: int,
    mover: SC.Mover,
    status: str,
    verdict: CK.Verdict | None = None,
    radar_id: int | None = None,
) -> None:
    for x in mover.flagged:
        M.add_move(
            conn,
            run_id,
            ticker=mover.ticker,
            company=mover.company,
            session=x.session,
            day=x.day.isoformat(),
            pct=round(x.pct, 2),
            price_from=x.start,
            price_to=x.end,
            volume=x.volume,
            status=status,
            cause=verdict.cause if verdict else "",
            what_happened=verdict.what_happened if verdict else "",
            title=verdict.title if verdict else "",
            radar_id=radar_id,
        )


def _may_repeat(conn: sqlite3.Connection, radar_id: int) -> list[str]:
    """What a mover's radar topic may repeat among the studio's recent pieces and queued
    topics (studio/repeats.py): such a topic stays on the radar, flagged, and is not
    queued on its own."""
    from studio import repeats as RP
    from studio import store as S
    from studio.runner import covered_for_repeats
    from studio.settings import load_studio_config

    topic = S.get_radar_topic(conn, radar_id)
    if topic is None:
        return []
    covered = covered_for_repeats(conn, load_studio_config())
    return [r.line() for r in RP.find(RP.topic_marks(topic), covered)]


def to_studio(
    conn: sqlite3.Connection,
    cfg: dict[str, Any],
    verdicts: list[CK.Verdict],
    by_ticker: dict[str, SC.Mover],
    *,
    screened: str,
) -> tuple[dict[str, int], int]:
    """Each verdict worth a piece as a radar topic, best first, and the best `auto_queue`
    queued for the studio. Returns ({ticker: radar id}, how many were queued)."""
    from studio import radar as R
    from studio import store as S
    from studio.settings import load_studio_config

    radar_ids: dict[str, int] = {}
    for v in CK.best_first(verdicts):
        m = by_ticker[v.ticker]
        moved = "; ".join(x.line() for x in m.flagged)
        topic = R.Topic(
            title=v.title,
            why_now=f"Market mover: {m.ticker} {moved}. {v.what_happened} {v.why_now}".strip()[
                : R.MAX_TEXT
            ],
            angle=v.angle,
            companies=(R.Company(m.company or m.ticker, m.ticker),),
            sources=v.sources,
        )
        radar_ids[v.ticker] = S.add_radar_topics(conn, RADAR_SCAN_ID, [topic])[0]
    queued = 0
    limit = int(cfg.get("auto_queue") or 0)
    studio_auto = bool(load_studio_config()["auto"].get("enabled"))
    if limit and not studio_auto:
        log.info("no mover queued: the studio's automatic pieces are off (studio/config.yaml)")
    for v in CK.best_first(verdicts):
        if queued >= limit or not studio_auto:
            break
        rid = radar_ids[v.ticker]
        try:
            repeats = _may_repeat(conn, rid)
        except Exception as exc:  # noqa: BLE001 - a flag, never a reason to stop
            log.warning("%s: could not check for repeats (%s)", v.ticker, exc)
            repeats = []
        if repeats:
            log.info("%s stays on the radar: it may repeat %s", v.ticker, "; ".join(repeats))
            continue
        text = CK.topic_text(v, by_ticker[v.ticker], screened=screened)
        topic_id = S.queue_topic(conn, topic=text, angle="", checkpoint=bool(cfg["checkpoint"]))
        S.set_radar_topic(conn, rid, status=S.RADAR_QUEUED, topic_id=topic_id)
        queued += 1
        log.info("%s queued for the studio (topic %s): %s", v.ticker, topic_id, v.title)
    return radar_ids, queued


def run(
    *,
    force: bool = False,
    dry_run: bool = False,
    now: datetime | None = None,
    fetch: PR.Fetch | None = None,
    call: Callable[..., str] | None = None,
    sleep: Callable[[float], None] | None = None,
) -> int:
    """One screen. 0 when it ran (or was not due), 1 when the check failed."""
    import claude_cli
    from ops import lock
    from studio import angles as A

    cfg = load_movers_config()
    if not cfg.get("enabled"):
        log.info("market movers are off (movers/config.yaml enabled)")
        return 0
    now = now or datetime.now(UTC)
    conn = _db_conn()
    held = None
    try:
        if not dry_run and not force:
            ok, why = due(M.last_run(conn, M.RUN_DONE), now, cfg)
            if not ok:
                log.info("no screen: %s", why)
                return 0
        if not dry_run:
            held = lock.acquire(_data_folder() / LOCK_NAME, trust_os_lock=True)
            if held is None:
                log.info("another screen is in progress; nothing to do")
                return 0
            M.close_running(conn, "the run stopped before the screen finished")
        root_cfg = _root_config()
        names = SC.universe(root_cfg, cfg)
        run_id = None if dry_run else M.start_run(conn)
        log.info("screening %d tickers", len(names))
        movers, failed, bench = screen_all(names, cfg, now=now, fetch=fetch, sleep=sleep)
        new = _new_only(conn, movers)
        log.info(
            "%d mover(s) of %s%% or more, %d new; %d ticker(s) without prices",
            len(movers),
            cfg["threshold_pct"],
            len(new),
            len(failed),
        )
        for m in new:
            log.info("  %s %s", m.ticker, "; ".join(x.line() for x in m.flagged))
        counts: dict[str, Any] = {
            "screened": len(names) - len(failed),
            "failed": len(failed),
            "flagged": len(new),
        }
        if failed:
            counts["detail"] = "no prices: " + ", ".join(failed[:40])
        cap = int(cfg["check"]["max_checked"])
        asked, over = new[:cap], new[cap:]
        today = now.astimezone(ZoneInfo(SC.MARKET_TZ)).date().isoformat()
        angles = {key: a.question for key, a in A.load_angles().items()}
        since = (now - timedelta(days=float(cfg["check"]["recent_days"]))).isoformat()
        system, user = CK.check_prompt(
            asked,
            today=today,
            threshold=float(cfg["threshold_pct"]),
            benchmark=SC.benchmark_line(bench, str(cfg.get("benchmark") or "").upper()),
            angles=angles,
            earlier=M.earlier_stories(conn, since),
        )
        if dry_run:
            if asked:
                print("--- the story check's system prompt ---\n" + system)
                print("\n--- and its user prompt ---\n" + user)
            else:
                print("no new movers: no story check would run")
            return 0
        assert run_id is not None
        for m in over:
            _record(conn, run_id, m, M.UNCHECKED)
        if not asked:
            M.finish_run(conn, run_id, status=M.RUN_DONE, **counts)
            return 0
        ccfg = cfg["check"]
        try:
            reply = (call or CK.call_checker)(
                system,
                user,
                model=str(ccfg["model"]),
                effort=str(ccfg["effort"]),
                root_cfg=root_cfg,
                timeout=float(ccfg["timeout_minutes"] or 0) * 60 or 1200.0,
            )
            result = CK.parse_check(reply, tickers=[m.ticker for m in asked], angles=angles)
        except (claude_cli.ClaudeCliError, CK.CheckRejected) as exc:
            # Nothing recorded for the movers asked about: the next screen asks again.
            log.error("the story check failed: %s", exc)
            M.finish_run(conn, run_id, status=M.RUN_FAILED, **counts, detail=str(exc)[:500])
            return 1
        by_ticker = {m.ticker: m for m in asked}
        radar_ids, queued = to_studio(conn, cfg, result.verdicts, by_ticker, screened=today)
        verdicts = {v.ticker: v for v in result.verdicts}
        for m in asked:
            v = verdicts.get(m.ticker)
            if v is None:
                status = M.UNCHECKED
            elif v.worth:
                status = M.STORY
            else:
                status = M.NOTED if v.story else M.NO_STORY
            _record(conn, run_id, m, status, v, radar_ids.get(m.ticker))
            if v is not None:
                log.info("  %s: %s (%s) %s", m.ticker, status, v.cause, v.what_happened)
        dropped = "; ".join(result.dropped[:10])
        M.finish_run(
            conn,
            run_id,
            status=M.RUN_DONE,
            **{**counts, "detail": "; ".join(x for x in (counts.get("detail"), dropped) if x)},
            checked=len(asked),
            topics=len(radar_ids),
            queued=queued,
            summary=result.summary,
        )
        log.info(
            "%d checked, %d worth a piece (on the radar), %d queued for the studio",
            len(asked),
            len(radar_ids),
            queued,
        )
        return 0
    finally:
        if held is not None:
            held.release()
        conn.close()
