"""CLI: publish approved drafts to X on a slot schedule. Safe by default.

Usage: python run_publish.py [--dry-run | --live] [--now] [--breaking] [--limit N]
                             [--format single|thread] [-v]
       python run_publish.py --release-failed [DRAFT_ID ...]

Default is --dry-run: prints what WOULD be posted and when, posts nothing. --live posts
only if PUBLISH_ENABLED=1 is also set in the environment. Meant for cron every 15 min; a
draft is claimed in a transaction before posting, so overlapping runs cannot post it twice.

A draft whose chart was rendered (run_draft.py) and not dropped in the queue has that PNG
attached to its first post, with alt text, unless media.attach_images is false in
publish/config.yaml. The dry run prints the image path and alt text.

A draft whose attempt failed before anything went live (status 'failed' or 'refused') stays
claimed and is never retried on its own. --release-failed drops those claims (all of them, or
the given draft ids) so the next run considers the drafts again; it posts nothing and never
touches a 'posted' or 'partial' row, since a partial thread is live on X.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import UTC, datetime, time
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

from publish import client, store
from publish.scheduler import (
    Policy,
    is_breaking,
    load_publish_config,
    next_slot,
    open_slot,
    pick_for_slot,
    rank,
    slot_label,
)
from publish.store import Approved
from publish.thread import ThreadError, check_post, split_thread

log = logging.getLogger("run_publish")

BIO_WARNING = (
    "BIO_DISCLOSURE_CONFIRMED is not set. The account bio must disclose AI-assisted drafting "
    "before anything is published (manual step; see README 'Bio disclosure')."
)


def live_enabled(args: argparse.Namespace) -> bool:
    """Nothing posts unless BOTH --live is passed AND PUBLISH_ENABLED=1."""
    if not args.live:
        return False
    if os.environ.get("PUBLISH_ENABLED") != "1":
        log.warning("--live given but PUBLISH_ENABLED != 1; staying in dry-run mode")
        return False
    return True


def build_policy(conn, cfg: dict, now: datetime) -> Policy:
    tz = ZoneInfo(cfg["timezone"])
    day_start = datetime.combine(now.astimezone(tz).date(), time(0), tzinfo=tz)
    return Policy(
        max_posts_per_day=int(cfg["max_posts_per_day"]),
        min_gap_minutes=int(cfg["min_gap_minutes"]),
        posted_today=len(store.posted_since(conn, day_start)),
        last_posted_at=store.last_posted_at(conn),
        prefer_breaking=bool(cfg["policy"]["prefer_breaking"]),
        order=str(cfg["policy"]["order"]),
        breaking=cfg["breaking"],
    )


def texts_for(approved: Approved, fmt: str) -> tuple[str, list[str]]:
    """(kind, ordered texts). Raises ThreadError if the content fails a hard check."""
    if fmt == store.KIND_THREAD and approved.thread:
        return store.KIND_THREAD, split_thread(approved.thread, url=approved.url)
    problems = check_post(approved.single_post, url=approved.url or None)
    if problems:
        raise ThreadError("single_post: " + "; ".join(problems))
    return store.KIND_SINGLE, [approved.single_post]


def choose(
    approved: list[Approved], cfg: dict, policy: Policy, now: datetime, args: argparse.Namespace
) -> tuple[Approved | None, str | None, str]:
    """(candidate, slot label, reason). candidate is None when nothing should post now."""
    tz, slots = cfg["timezone"], cfg["slots"]
    if not approved:
        return None, None, "no approved drafts waiting"
    blocked = policy.blocked_reason(now)
    if args.now:
        if blocked:
            return None, None, f"--now blocked: {blocked}"
        return rank(approved, policy)[0], "now", "--now: top candidate"
    breaking = [a for a in approved if is_breaking(a, cfg["breaking"])]
    if args.breaking and not breaking:
        return None, None, "no breaking items among approved drafts"
    if breaking:
        if blocked:
            return None, None, f"breaking item waiting but {blocked}"
        return rank(breaking, policy)[0], "breaking", "breaking news, posting outside slots"
    if args.breaking:
        return None, None, "no breaking items"
    slot = open_slot(now, slots, tz, int(cfg["grace_minutes"]))
    if slot is None:
        nxt = next_slot(now, slots, tz)
        return None, slot_label(nxt), f"no slot open; next slot {slot_label(nxt)} {nxt.tzname()}"
    label = slot_label(slot)
    if blocked:
        return None, label, f"slot {label} open but {blocked}"
    return pick_for_slot(approved, slot, policy), label, f"slot {label} open"


def image_for(approved: Approved, cfg: dict) -> str | None:
    """The PNG to attach to the first post, or None (no image, or media.attach_images false)."""
    if not (cfg.get("media") or {}).get("attach_images", True):
        return None
    return approved.image_path or None


def publish_one(
    conn,
    approved: Approved,
    kind: str,
    texts: list[str],
    slot: str,
    now: datetime,
    image: str | None = None,
) -> str:
    """Post texts in order, chaining replies; `image` is attached to the first post. Returns
    the schedule status. An image upload failure happens before any tweet, so the draft is
    simply 'failed' and nothing is live."""
    prev: str | None = None
    for pos, text in enumerate(texts, 1):
        try:
            media_ids = None
            if pos == 1 and image:
                media_ids = [client.upload_media(image, approved.image_alt)]
                log.info("draft %d: image uploaded (%s)", approved.draft_id, image)
            tweet_id = client.post_tweet(text, in_reply_to=prev, media_ids=media_ids)
        except client.PublishError as exc:
            store.record_post(
                conn,
                draft_id=approved.draft_id,
                text=text,
                kind=kind,
                position=pos,
                slot=slot,
                error=str(exc),
            )
            if pos == 1:
                store.finish(conn, approved.draft_id, store.SCHED_FAILED, str(exc))
                log.error("draft %d: post 1 failed, nothing posted: %s", approved.draft_id, exc)
                return store.SCHED_FAILED
            store.finish(conn, approved.draft_id, store.SCHED_PARTIAL, str(exc))
            log.error(
                "draft %d: THREAD PARTIAL — posts 1..%d are live, post %d failed: %s. "
                "Not retrying automatically; a human must finish or delete the thread.",
                approved.draft_id,
                pos - 1,
                pos,
                exc,
            )
            return store.SCHED_PARTIAL
        store.record_post(
            conn,
            draft_id=approved.draft_id,
            text=text,
            kind=kind,
            position=pos,
            slot=slot,
            tweet_id=tweet_id,
            posted_at=now,
        )
        log.info(
            "posted draft %d %s %d/%d tweet_id=%s",
            approved.draft_id,
            kind,
            pos,
            len(texts),
            tweet_id,
        )
        prev = tweet_id
    store.finish(conn, approved.draft_id, store.SCHED_POSTED)
    return store.SCHED_POSTED


def release_failed(draft_ids: list[int]) -> int:
    """--release-failed: drop the claims of drafts whose attempt posted nothing."""
    conn = store.connect()
    try:
        released = store.release_failed(conn, draft_ids or None)
    finally:
        conn.close()
    if not released:
        print("nothing to release: no failed or refused claim without a live post")
        return 0
    for did in released:
        print(f"released draft {did}: approved again, considered on the next run")
    return 0


def main(argv: list[str] | None = None, now: datetime | None = None) -> int:
    load_dotenv()
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument(
        "--dry-run", action="store_true", help="(default) print the plan, post nothing"
    )
    mode.add_argument("--live", action="store_true", help="post, if PUBLISH_ENABLED=1")
    ap.add_argument("--now", action="store_true", help="ignore slots; post the top candidate")
    ap.add_argument("--breaking", action="store_true", help="post only breaking items")
    ap.add_argument("--limit", type=int, default=10, help="max approved drafts to consider")
    ap.add_argument("--format", choices=("single", "thread"), default=None)
    ap.add_argument("--config", default=None, help="path to publish/config.yaml override")
    ap.add_argument(
        "--release-failed",
        nargs="*",
        type=int,
        metavar="DRAFT_ID",
        default=None,
        help="release failed/refused claims (all, or these draft ids) so they can be retried; "
        "posts nothing",
    )
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    if args.release_failed is not None:
        return release_failed(args.release_failed)
    if os.environ.get("BIO_DISCLOSURE_CONFIRMED") != "1":
        log.warning(BIO_WARNING)

    live = live_enabled(args)
    cfg = load_publish_config(args.config)
    fmt = args.format or cfg["post_format"]
    now = now or datetime.now(UTC)
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")

    conn = store.connect()
    try:
        approved = store.fetch_approved(args.limit, conn=conn)
        policy = build_policy(conn, cfg, now)
        log.info(
            "%d approved drafts waiting; %d/%d posted today; mode=%s",
            len(approved),
            policy.posted_today,
            policy.max_posts_per_day,
            "LIVE" if live else "dry-run",
        )
        cand, slot, reason = choose(approved, cfg, policy, now, args)
        if cand is None:
            log.info("nothing to post: %s", reason)
            return 0
        assert slot is not None
        try:
            kind, texts = texts_for(cand, fmt)
        except ThreadError as exc:
            log.warning("draft %d refused, fix it in the approval queue: %s", cand.draft_id, exc)
            if live and store.claim(conn, cand.draft_id, slot):
                store.finish(conn, cand.draft_id, store.SCHED_REFUSED, str(exc))
            return 0

        image = image_for(cand, cfg)
        if not live:
            print(f"DRY RUN — would post draft {cand.draft_id} ({kind}, {reason}) [{cand.source}]")
            for i, t in enumerate(texts, 1):
                print(f"  {i}/{len(texts)}: {t}")
            if image:
                print(f"  image on post 1: {image}")
                print(f"  alt text: {cand.image_alt}")
            return 0

        if not store.claim(conn, cand.draft_id, slot):
            log.info("draft %d already claimed by another run; skipping", cand.draft_id)
            return 0
        log.info("claimed draft %d for %s (%s)", cand.draft_id, slot, reason)
        status = publish_one(conn, cand, kind, texts, slot, now, image=image)
        return 0 if status == store.SCHED_POSTED else 2
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
