"""CLI (step 9, phase two): score the swarm's posted drafts against X and prune genomes.

Usage: python run_evolve.py [score|prune|report] [--kpi impressions] [--baseline-days 30]
                            [--min-posts 5] [--dry-run] [-v]

No sub-command runs score, then prune, then report. No network: reads step 3's `posts` and
step 4's `tweet_metrics` (fill them with `run_feedback.py snapshot`) through
swarm.store.fetch_head_metrics, writes only swarm_fitness and swarm_genomes.retired_at.

score   Every posted swarm run gets the head tweet's KPI, the median KPI of the posts in the
        trailing `baseline_days` before it (the baseline) and value / baseline (relative),
        stored in swarm_fitness. Fewer than `min_baseline_posts` earlier posts: no relative
        score yet.
prune   A live genome with at least `min_posts` scored posts whose median relative KPI is
        below the median of every such genome is retired (swarm_genomes.retired_at and
        retired_reason), never below `min_alive` live genomes. --dry-run prints and keeps.
report  Per-genome table and the swarm-vs-control measurement: median relative KPI of the
        posts the jury gave to the swarm against those it gave to the control.
"""

from __future__ import annotations

import argparse
import logging
import sys

from dotenv import load_dotenv

from approval_queue import store as queue_store
from swarm import fitness
from swarm import store as swarm_store
from swarm.settings import load_swarm_config

log = logging.getLogger("run_evolve")


def observations(conn) -> list[fitness.Observation]:
    """Stored fitness rows as Observations (for prune/report without a fresh score)."""
    return [
        fitness.Observation(
            run_id=r.run_id,
            draft_id=r.draft_id,
            genome_id=r.genome_id,
            winner=r.winner,
            posted_at=fitness.parse_when(r.posted_at),
            value=r.value,
            baseline=r.baseline,
            relative=r.relative,
        )
        for r in swarm_store.list_fitness(conn)
    ]


def cmd_score(conn, cfg: dict) -> list[fitness.Observation]:
    kpi = str(cfg["kpi"])
    if kpi not in swarm_store.KPIS:
        raise SystemExit(f"unknown kpi {kpi!r}; one of {swarm_store.KPIS}")
    heads = swarm_store.fetch_head_metrics(conn)
    obs = [
        fitness.Observation(
            run_id=h.run_id,
            draft_id=h.draft_id,
            genome_id=h.genome_id,
            winner=h.winner,
            posted_at=fitness.parse_when(h.posted_at),
            value=float(h.metrics[kpi]),
        )
        for h in heads
    ]
    scored = fitness.score(
        obs,
        baseline_days=float(cfg["baseline_days"]),
        min_baseline_posts=int(cfg["min_baseline_posts"]),
    )
    for o, h in zip(scored, sorted(heads, key=lambda h: (h.posted_at, h.run_id)), strict=True):
        swarm_store.upsert_fitness(
            conn,
            run_id=o.run_id,
            draft_id=o.draft_id,
            genome_id=o.genome_id,
            winner=o.winner,
            tweet_id=h.tweet_id,
            posted_at=h.posted_at,
            kpi=kpi,
            value=o.value,
            baseline=o.baseline,
            relative=o.relative,
        )
    n_rel = sum(o.relative is not None for o in scored)
    log.info(
        "scored %d posted swarm runs on %s (%d with a relative score)", len(scored), kpi, n_rel
    )
    return scored


def cmd_prune(conn, cfg: dict, obs: list[fitness.Observation], *, dry_run: bool) -> list[int]:
    live = [g.id for g in swarm_store.live_genomes(conn)]
    decision = fitness.prune(
        fitness.genome_scores(obs),
        live,
        min_posts=int(cfg["min_posts"]),
        min_alive=int(cfg["min_alive"]),
    )
    names = swarm_store.genome_names(conn)
    if not decision.retire:
        log.info("prune: nothing to retire (%d live genomes)", len(live))
        return []
    for gid in decision.retire:
        reason = decision.reason[gid]
        if dry_run:
            log.info("dry run: would retire genome %d %s: %s", gid, names.get(gid, "?"), reason)
        else:
            swarm_store.retire_genome(conn, gid, reason)
            log.info("retired genome %d %s: %s", gid, names.get(gid, "?"), reason)
    return decision.retire


def render_report(conn, cfg: dict, obs: list[fitness.Observation]) -> str:
    names = swarm_store.genome_names(conn)
    live = {g.id for g in swarm_store.live_genomes(conn)}
    head = f"Swarm fitness on {cfg['kpi']}"
    lines = [f"{head} (relative to the trailing {cfg['baseline_days']}-day median)"]
    lines.append("")
    lines.append(f"{'genome':<12} {'state':<8} {'posts':>5} {'median rel':>10}")
    for s in fitness.genome_scores(obs):
        name = names.get(s.genome_id, "?") if s.genome_id is not None else "-"
        state = "live" if s.genome_id in live else "retired"
        med = f"{s.median_relative:.2f}" if s.median_relative is not None else "-"
        lines.append(f"{name:<12} {state:<8} {s.n:>5} {med:>10}")
    for gid in sorted(live):
        if all(s.genome_id != gid for s in fitness.genome_scores(obs)):
            lines.append(f"{names.get(gid, '?'):<12} {'live':<8} {0:>5} {'-':>10}")
    b = fitness.bet_summary(obs)
    lines.append("")
    lines.append("Swarm vs control (posts by who won the jury):")

    def fmt(n: int, m: float | None) -> str:
        return f"{n} posts, median relative {m:.2f}" if m is not None else f"{n} posts"

    lines.append(f"  swarm won:   {fmt(b.swarm_n, b.swarm_median)}")
    lines.append(f"  control won: {fmt(b.control_n, b.control_median)}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("command", nargs="?", choices=["score", "prune", "report"], default=None)
    ap.add_argument("--kpi", default=None, help="override evolve.kpi in swarm/config.yaml")
    ap.add_argument("--baseline-days", type=float, default=None)
    ap.add_argument("--min-posts", type=int, default=None)
    ap.add_argument("--dry-run", action="store_true", help="prune: print, retire nothing")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    cfg = dict(load_swarm_config()["evolve"])
    if args.kpi:
        cfg["kpi"] = args.kpi
    if args.baseline_days is not None:
        cfg["baseline_days"] = args.baseline_days
    if args.min_posts is not None:
        cfg["min_posts"] = args.min_posts

    conn = queue_store.connect()
    try:
        swarm_store.ensure_tables(conn)
        swarm_store.seed_default(conn)
        if args.command in (None, "score"):
            obs = cmd_score(conn, cfg)
        else:
            obs = observations(conn)
        if args.command in (None, "prune"):
            cmd_prune(conn, cfg, obs, dry_run=args.dry_run)
        if args.command in (None, "report"):
            print(render_report(conn, cfg, obs))
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
