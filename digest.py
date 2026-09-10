#!/usr/bin/env python3
"""Print the top-N scored clusters from the last window as markdown.

`--rate` walks the same list interactively as the editor: yes or no, post this story
or not, plus an explanation that starts with the deciding reason category
(`score/editorial.py:REASON_CATEGORIES`, printed as a hint before the note prompt).
Each decision is stored in the ratings table (yes = 5, no = 1; training data for rubric
tuning). `--auto-rate` asks the model in config.yaml `models.rater` the same yes/no
question and stores its answer as rater='auto:<model>', a second opinion shown next to
the prompt in `--rate`; human decisions stay the ground truth."""

from __future__ import annotations

import argparse
import logging
import sys
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from config import load_config, setup_logging
from db import Cluster, Database, Score, window_start
from filter.prefilter import cluster_text
from score import editorial, rater

log = logging.getLogger("digest")


def render_entry(rank: int, db: Database, cl: Cluster, sc: Score) -> str:
    items = db.items_in_cluster(cl.id)
    title, abstract = cluster_text(items) if items else (cl.title, "")
    primary = items[0] if items else None
    when = cl.published_at.strftime("%Y-%m-%d") if cl.published_at else "n/a"
    lines = [f"## {rank}. {title}", ""]
    lines.append(
        f"**Score {sc.total}/50** · {sc.evidence_level} · hype {sc.hype_risk}/10 · "
        f"novelty {sc.novelty} · clinical {sc.clinical_significance} · audience "
        f"{sc.audience_interest} · fit {sc.expertise_fit} · timely {sc.timeliness}"
    )
    lines.append("")
    if primary:
        lines.append(f"- Source: {primary.source} · {when} · <{primary.url}>")
    if cl.doi:
        lines.append(f"- DOI: https://doi.org/{cl.doi}")
    if len(items) > 1:
        lines.append(f"- Also covered by: {', '.join(sorted({i.source for i in items[1:]}))}")
    lines.append(f"- Rationale: {sc.rationale}")
    lines.append(f"- Angle: {sc.suggested_angle}")
    if abstract:
        lines.append("")
        lines.append(f"> {abstract[:500]}{'…' if len(abstract) > 500 else ''}")
    lines.append("")
    return "\n".join(lines)


def build_digest(
    db: Database,
    cfg: dict[str, Any],
    *,
    top_n: int | None = None,
    hours: int | None = None,
    min_total: int | None = None,
) -> tuple[str, list[tuple[Cluster, Score]]]:
    dcfg = cfg.get("digest") or {}
    top_n = top_n or int(dcfg.get("top_n", 10))
    hours = hours or int(dcfg.get("window_hours", 24))
    if min_total is None:
        min_total = int((cfg.get("scoring") or {}).get("threshold", 0))
    rows = db.top_scored_clusters(window_start(hours), top_n, min_total)
    head = [f"# Cancer research digest — last {hours}h, top {top_n}, threshold {min_total}", ""]
    if not rows:
        head.append("_No scored clusters in window._")
    body = [render_entry(i + 1, db, cl, sc) for i, (cl, sc) in enumerate(rows)]
    return "\n".join(head + body), rows


def _parse_ts(value: Any) -> datetime:
    dt = datetime.fromisoformat(str(value)) if value else datetime.min.replace(tzinfo=UTC)
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def _auto_ratings(db: Database, cluster_id: int) -> list[dict[str, Any]]:
    return [r for r in db.ratings_for(cluster_id) if (r.get("rater") or "human") != "human"]


def rate(db: Database, rows: list[tuple[Cluster, Score]], ask: Callable[[str], str] = input) -> int:
    """Ask yes/no (post this?) and an explanation per cluster. Returns number saved."""
    saved = 0
    for i, (cl, sc) in enumerate(rows):
        print(render_entry(i + 1, db, cl, sc))
        for r in _auto_ratings(db, cl.id)[-1:]:
            print(
                f"model decision ({r['rater']}): {editorial.decision_of(r['rating'])} "
                f"— {r.get('note') or ''}"
            )
        while True:
            ans = ask("post this? y/n (s=skip, q=quit): ").strip().lower()
            if ans in ("q", "quit"):
                return saved
            if ans in ("s", "skip", ""):
                break
            try:
                decision = editorial.parse_decision(ans)
            except ValueError:
                print("enter y, n, s, or q")
                continue
            print(editorial.reasons_text())
            note = ask("why: ").strip()
            while not note:
                note = ask("why (an explanation is required): ").strip()
            db.insert_rating(cl.id, editorial.rating_for(decision), note)
            saved += 1
            break
    return saved


def auto_rate(
    db: Database,
    cfg: dict[str, Any],
    rows: list[tuple[Cluster, Score]],
    call: rater.CallFn = rater.call_model,
) -> int:
    """Store one model rating per entry. An entry is skipped when this model already rated
    it after its latest score; a re-score (rubric bump) gets a fresh rating. Returns number
    saved."""
    name = rater.rater_name(cfg)
    saved = 0
    for i, (cl, sc) in enumerate(rows):
        mine = [r for r in db.ratings_for(cl.id) if r.get("rater") == name]
        if mine and _parse_ts(mine[-1].get("rated_at")) >= sc.scored_at:
            log.debug("cluster %s already rated by %s since its last score", cl.id, name)
            continue  # a newer score (rubric bump) gets a fresh rating
        items = db.items_in_cluster(cl.id)
        title, abstract = cluster_text(items) if items else (cl.title, "")
        entry = {
            "source": items[0].source if items else "?",
            "published_at": cl.published_at.isoformat() if cl.published_at else None,
            "title": title,
            "abstract": abstract,
            "total": sc.total,
            "hype_risk": sc.hype_risk,
            "rationale": sc.rationale,
            "suggested_angle": sc.suggested_angle,
        }
        try:
            ar = rater.rate_entry(entry, cfg, call=call)
        except Exception as exc:  # one bad reply must not stop the run
            log.error("%d. %s: rating failed: %s", i + 1, title[:70], exc)
            continue
        db.insert_rating(cl.id, ar.rating, ar.note, rater=name)
        saved += 1
        print(f"{i + 1}. [{ar.decision}] {title[:90]}\n   {ar.note}")
    log.info("saved %d model ratings (%s)", saved, name)
    return saved


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--config", default=None)
    ap.add_argument("--top", type=int, default=None)
    ap.add_argument("--hours", type=int, default=None)
    ap.add_argument("--all", action="store_true", help="ignore the score threshold")
    ap.add_argument("--rate", action="store_true", help="decide yes/no per entry, with a reason")
    ap.add_argument(
        "--auto-rate",
        action="store_true",
        help="have config.yaml models.rater decide yes/no per entry (rater='auto:<model>')",
    )
    ap.add_argument("--out", default=None, help="write markdown to this file")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)
    setup_logging(args.verbose)
    cfg = load_config(args.config)
    db = Database(cfg.get("db_path", "pipeline.db"))
    try:
        md, rows = build_digest(
            db, cfg, top_n=args.top, hours=args.hours, min_total=0 if args.all else None
        )
        if args.auto_rate:
            auto_rate(db, cfg, rows)
            if not args.rate:
                return 0
        if args.rate:
            n = rate(db, rows)
            log.info("saved %d decisions", n)
            return 0
        if args.out:
            with open(args.out, "w", encoding="utf-8") as fh:
                fh.write(md + "\n")
            log.info("wrote %s", args.out)
        else:
            print(md)
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
