#!/usr/bin/env python3
"""Print the top-N scored clusters from the last window as markdown.

`--rate` walks the same list interactively and stores a 1-5 rating plus a note
per cluster in the ratings table (training data for rubric tuning)."""

from __future__ import annotations

import argparse
import logging
import sys
from collections.abc import Callable
from typing import Any

from config import load_config, setup_logging
from db import Cluster, Database, Score, window_start
from filter.prefilter import cluster_text

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


def rate(db: Database, rows: list[tuple[Cluster, Score]], ask: Callable[[str], str] = input) -> int:
    """Prompt for a 1-5 rating and note per cluster. Returns number saved."""
    saved = 0
    for i, (cl, sc) in enumerate(rows):
        print(render_entry(i + 1, db, cl, sc))
        while True:
            ans = ask("rating 1-5 (s=skip, q=quit): ").strip().lower()
            if ans in ("q", "quit"):
                return saved
            if ans in ("s", "skip", ""):
                break
            if ans.isdigit() and 1 <= int(ans) <= 5:
                note = ask("note (optional): ").strip() or None
                db.insert_rating(cl.id, int(ans), note)
                saved += 1
                break
            print("enter 1-5, s, or q")
    return saved


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--config", default=None)
    ap.add_argument("--top", type=int, default=None)
    ap.add_argument("--hours", type=int, default=None)
    ap.add_argument("--all", action="store_true", help="ignore the score threshold")
    ap.add_argument("--rate", action="store_true", help="interactively rate each entry")
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
        if args.rate:
            n = rate(db, rows)
            log.info("saved %d ratings", n)
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
