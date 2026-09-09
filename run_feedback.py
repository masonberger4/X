"""CLI: the feedback loop (step 4). Read-only against X; proposes rubric changes, applies none.

Usage:
  python run_feedback.py snapshot [--all] [--dry-run] [-v]
      Pull public_metrics for every tweet due per feedback/config.yaml (daily for the first
      days, weekly after, never after stop_after_days) plus one follower snapshot per day.
      Idempotent: a second run the same day fetches nothing. --all ignores the schedule.
      --dry-run prints the ids it would fetch and touches nothing.
  python run_feedback.py report [--weeks N] [--out FILE] [-v]
      Render the markdown report from stored snapshots only (no network) and store it in
      feedback_reports. Prints to stdout unless --out is given.
  python run_feedback.py followers
      Print the follower time series.

Cron: `snapshot` daily, `report` weekly.
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

from feedback import client, store
from feedback.analysis import analyse
from feedback.report import render
from feedback.suggest import suggest

log = logging.getLogger("run_feedback")

CONFIG_PATH = Path(__file__).resolve().parent / "feedback" / "config.yaml"


def load_feedback_config(path: str | Path | None = None) -> dict[str, Any]:
    with open(path or CONFIG_PATH, encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-5s %(name)s: %(message)s",
    )
    logging.getLogger("httpx").setLevel(logging.DEBUG if verbose else logging.WARNING)


# ---------------------------------------------------------------------------
# snapshot
# ---------------------------------------------------------------------------


def cmd_snapshot(args: argparse.Namespace, cfg: dict[str, Any], now: datetime) -> int:
    conn = store.connect()
    try:
        schedule = store.Schedule.from_config(cfg)
        due = store.due_for_snapshot(now, conn, schedule, ignore_schedule=args.all)
        username = (cfg.get("username") or "").lstrip("@")
        want_followers = bool(username) and not store.has_follower_snapshot(conn, now)
        log.info(
            "tweets due: %d (%s); follower snapshot: %s",
            len(due),
            "schedule ignored" if args.all else "per schedule",
            "due" if want_followers else ("done today" if username else "no username"),
        )
        if args.dry_run:
            for p in due:
                print(f"{p.tweet_id}\tdraft {p.draft_id}\t{p.kind} #{p.position}\t{p.posted_at}")
            if want_followers:
                print(f"followers\t@{username}")
            print(f"DRY RUN: {len(due)} tweet(s) would be fetched; nothing stored")
            return 0
        rc = 0
        fetched = deleted = 0
        if due:
            by_id = {p.tweet_id: p for p in due}
            try:
                metrics = client.get_tweet_metrics(list(by_id), cfg)
            except client.FeedbackAPIError as exc:
                log.error("tweet metrics fetch failed: %s", exc)
                rc = 1
            else:
                for tid, tm in metrics.items():
                    p = by_id.get(tid)
                    if p is None:
                        continue
                    store.record_tweet_metrics(conn, p.draft_id, tm, now)
                    fetched += 1
                    deleted += 1 if tm.deleted else 0
        log.info("fetched: %d, deleted: %d", fetched, deleted)
        if want_followers:
            try:
                um = client.get_user_metrics(username, cfg)
            except client.FeedbackAPIError as exc:
                log.error("follower fetch failed: %s", exc)
                rc = 1
            else:
                store.record_follower_snapshot(conn, um, now)
                log.info(
                    "followers: %d (following %d, tweets %d)",
                    um.followers,
                    um.following,
                    um.tweet_count,
                )
        elif not username:
            log.warning("feedback/config.yaml username is empty; skipping follower snapshot")
        return rc
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# report
# ---------------------------------------------------------------------------


def build_report(conn, cfg: dict[str, Any], now: datetime, weeks: int) -> tuple[str, list[dict]]:
    """Analysis + suggestions + markdown from stored rows only. No network."""
    report_cfg = cfg.get("report") or {}
    suggest_cfg = cfg.get("suggest") or {}
    min_posts = int(report_cfg.get("min_posts_per_group", 5))
    window_end = now
    window_start = now - timedelta(weeks=weeks)
    posted = store.fetch_posted(window_start, conn)
    posted = [p for p in posted if p.posted_at <= window_end]
    contexts = store.fetch_post_context({p.draft_id for p in posted}, conn)
    snapshots = store.fetch_tweet_snapshots(conn, [p.tweet_id for p in posted])
    followers = store.fetch_follower_snapshots(conn, window_start, window_end)
    analysis = analyse(
        posted,
        contexts,
        snapshots,
        followers,
        window_start=window_start,
        window_end=window_end,
        kpi=str(cfg.get("kpi") or "impressions"),
        timezone=str(cfg.get("timezone") or "UTC"),
        topics=cfg.get("topics") or {},
        top_n=int(report_cfg.get("top_n", 5)),
    )
    for dim, groups in analysis.groups.items():
        for g in groups:
            if g.n < min_posts:
                log.warning("small-n group %s=%s (n=%d < %d)", dim, g.group, g.n, min_posts)
    suggestions = suggest(
        analysis,
        min_posts=min_posts,
        effect_ratio=float(suggest_cfg.get("effect_ratio", 2.0)),
        min_abs_rho=float(suggest_cfg.get("min_abs_rho", 0.5)),
    )
    log.info(
        "report window %s..%s: %d posts, %d suggestions",
        window_start.date(),
        window_end.date(),
        analysis.n_posts,
        len(suggestions),
    )
    md = render(analysis, suggestions, min_posts=min_posts)
    return md, [s.__dict__ for s in suggestions]


def cmd_report(args: argparse.Namespace, cfg: dict[str, Any], now: datetime) -> int:
    weeks = int(args.weeks or (cfg.get("report") or {}).get("weeks", 1))
    conn = store.connect()
    try:
        md, suggestions = build_report(conn, cfg, now, weeks)
        store.record_report(
            conn,
            window_start=now - timedelta(weeks=weeks),
            window_end=now,
            report_md=md,
            suggestions=suggestions,
            now=now,
        )
    finally:
        conn.close()
    if args.out:
        Path(args.out).write_text(md, encoding="utf-8")
        log.info("wrote %s", args.out)
    else:
        print(md)
    return 0


# ---------------------------------------------------------------------------
# followers
# ---------------------------------------------------------------------------


def cmd_followers(args: argparse.Namespace, cfg: dict[str, Any], now: datetime) -> int:
    conn = store.connect()
    try:
        rows = store.fetch_follower_snapshots(conn)
    finally:
        conn.close()
    if not rows:
        print("no follower snapshots yet (run `snapshot` with a username in feedback/config.yaml)")
        return 0
    print("date\tfollowers\tdelta\tfollowing\ttweets")
    prev = None
    for r in rows:
        delta = "-" if prev is None else f"{r.followers - prev:+d}"
        print(f"{r.captured_on}\t{r.followers}\t{delta}\t{r.following}\t{r.tweet_count}")
        prev = r.followers
    return 0


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("-v", "--verbose", action="store_true")
    p.add_argument("--config", help="path to feedback/config.yaml (default: the packaged one)")
    sub = p.add_subparsers(dest="command", required=True)
    s = sub.add_parser("snapshot", help="pull metrics for tweets that are due")
    s.add_argument("--all", action="store_true", help="ignore the schedule (still one per day)")
    s.add_argument("--dry-run", action="store_true", help="print what would be fetched")
    s.set_defaults(func=cmd_snapshot)
    r = sub.add_parser("report", help="render the report from stored snapshots (no network)")
    r.add_argument("--weeks", type=int, default=None, help="window length (default from config)")
    r.add_argument("--out", help="write markdown here instead of stdout")
    r.set_defaults(func=cmd_report)
    f = sub.add_parser("followers", help="print the follower time series")
    f.set_defaults(func=cmd_followers)
    return p


def main(argv: list[str] | None = None, now: datetime | None = None) -> int:
    args = build_parser().parse_args(argv)
    setup_logging(args.verbose)
    load_dotenv()
    cfg = load_feedback_config(args.config)
    return int(args.func(args, cfg, now or store.utcnow()))


if __name__ == "__main__":
    sys.exit(main())
