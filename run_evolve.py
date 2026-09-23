"""CLI (step 9, phases two and three): score the swarm's posted drafts against X, prune
genomes, breed replacements.

Usage: python run_evolve.py [score|prune|breed|report] [--kpi impressions]
                            [--baseline-days 30] [--min-posts 5] [--dry-run] [--force] [-v]

No sub-command runs score, prune, breed, then report. Reads step 3's `posts` and step 4's
`tweet_metrics` (fill them with `run_feedback.py snapshot`) through
swarm.store.fetch_head_metrics; writes only swarm_fitness and swarm_genomes. The one
network call is `breed` for a writer child (swarm/mutate.py, through
draft.drafter.call_anthropic); everything else is offline.

score   Every posted swarm run gets the head tweet's KPI, read from its first snapshot at
        least `horizon_hours` old (a younger post is not scored yet), minus the account's own
        post-2 reply when `subtract_self_reply`; the median KPI of the posts in the trailing
        `baseline_days` before it (the baseline); and (value + smoothing) / (baseline +
        smoothing) (relative), stored in swarm_fitness. Fewer than `min_baseline_posts`
        earlier posts: no relative score yet. Credit: a writer genome is credited only with
        posts the jury gave the swarm in a format that ran its slot rules (not a single
        post), a designer only with posts whose chart run_draft drew in its Style;
        swarm_fitness.genome_id / designer_id are NULL otherwise. The table is rewritten to
        exactly what the current rules score (rows for runs not scored are dropped).
prune   `prune_rule: confidence` (shipped): among live genomes with at least `min_posts`
        (`format_min_posts` for a format) credited, scored posts, retire the worst one whose
        mean log relative is below the pooled rest's with probability at least
        1 - (1 - `retire_confidence`) / k (a t test; per look, and evolve looks again as
        posts accumulate), at most `max_retire_per_run` per kind per run, never below
        `min_alive`. `prune_rule: median` is the old rule (below the population median).
        Retirement is swarm_genomes.retired_at and retired_reason. --dry-run prints and
        keeps.
breed   Phase three. While fewer than `population_size` writer genomes (designers use
        `designer_population_size`, formats `format_population_size`) are live, breed one
        child per gap: a writer
        child by ONE strong-model call (`evolve.mutation_model`, blank = the drafting model)
        that reads the top genomes and their best posts and varies exactly one thing (code
        checks that it did); a designer child by stepping one Style knob at random; a
        format child (phase four) by stepping one field (shape, visual count, an anchor,
        the post range). Parents are the best-scoring live rows with at least `min_posts`
        scored posts, round-robin. Without any such genome nothing is bred unless --force
        (then the parents are simply the live rows). --dry-run prints and stores nothing.
report  Per-genome and per-designer tables, the family tree, and the swarm-vs-control
        measurement: median relative KPI of the posts the jury gave to the swarm against
        those it gave to the control.
"""

from __future__ import annotations

import argparse
import logging
import random
import sys

from dotenv import load_dotenv

from approval_queue import store as queue_store
from draft.drafter import model_name as drafting_model
from swarm import fitness, mutate
from swarm import store as swarm_store
from swarm.genome import Designer, FormatGenome, Genome
from swarm.settings import DEFAULTS, load_swarm_config

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
            designer_id=r.designer_id,
            format_id=r.format_id,
        )
        for r in swarm_store.list_fitness(conn)
    ]


def _opt(cfg: dict, key: str):
    """An evolve setting, falling back to swarm/settings.py DEFAULTS when a caller's config
    (an older file, a test) does not carry it."""
    return cfg.get(key, DEFAULTS["evolve"].get(key))


def credit(o: fitness.Observation) -> fitness.Observation:
    """Blank the genome ids that did not shape this post (fitness.writer_credited,
    designer_credited), so every later score, prune, report and the panel only ever count
    a genome's own posts. The format is always credited: both variants wrote to it."""
    if not fitness.writer_credited(o):
        o.genome_id = None
    if not fitness.designer_credited(o):
        o.designer_id = None
    return o


def cmd_score(conn, cfg: dict) -> list[fitness.Observation]:
    kpi = str(cfg["kpi"])
    if kpi not in swarm_store.KPIS:
        raise SystemExit(f"unknown kpi {kpi!r}; one of {swarm_store.KPIS}")
    heads = swarm_store.fetch_head_metrics(
        conn,
        horizon_hours=float(_opt(cfg, "horizon_hours") or 0),
        subtract_self_reply=bool(_opt(cfg, "subtract_self_reply")),
    )
    obs = [
        fitness.Observation(
            run_id=h.run_id,
            draft_id=h.draft_id,
            genome_id=h.genome_id,
            winner=h.winner,
            posted_at=fitness.parse_when(h.posted_at),
            value=float(h.metrics[kpi]),
            designer_id=h.designer_id,
            format_id=h.format_id,
            shape=h.shape,
            visuals=h.visuals,
            styled=h.styled,
        )
        for h in heads
    ]
    scored = fitness.score(
        obs,
        baseline_days=float(cfg["baseline_days"]),
        min_baseline_posts=int(cfg["min_baseline_posts"]),
        smoothing=float(_opt(cfg, "smoothing") or 0),
    )
    by_run = {h.run_id: h for h in heads}
    for o in scored:
        credit(o)
        h = by_run[o.run_id]
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
            designer_id=o.designer_id,
            format_id=o.format_id,
        )
    dropped = swarm_store.keep_fitness(conn, [o.run_id for o in scored])
    if dropped:
        log.info("dropped %d fitness row(s) the current rules no longer score", dropped)
    n_rel = sum(o.relative is not None for o in scored)
    n_writer = sum(o.genome_id is not None and o.relative is not None for o in scored)
    log.info(
        "scored %d posted swarm runs on %s (%d with a relative score, %d credited to a writer "
        "genome)",
        len(scored),
        kpi,
        n_rel,
        n_writer,
    )
    return scored


_KEY = {"writer": "genome_id", "designer": "designer_id", "format": "format_id"}


def _min_posts(cfg: dict, kind: str) -> int:
    """Formats are coarse genes and get their own, higher bar."""
    if kind == "format":
        return int(cfg.get("format_min_posts", _opt(cfg, "min_posts")))
    return int(_opt(cfg, "min_posts"))


def _population_size(cfg: dict, kind: str) -> int:
    if kind == "format":
        return int(cfg.get("format_population_size", cfg.get("population_size", 3)))
    if kind == "designer":
        return int(cfg.get("designer_population_size", cfg.get("population_size", 3)))
    return int(cfg.get("population_size", 3))


def _decide(cfg: dict, kind: str, obs: list[fitness.Observation], live: list[int]):
    scores = fitness.genome_scores(obs, _KEY[kind])
    if str(_opt(cfg, "prune_rule")) == "median":
        return fitness.prune(
            scores, live, min_posts=_min_posts(cfg, kind), min_alive=int(cfg["min_alive"])
        )
    return fitness.prune_confident(
        scores,
        live,
        min_posts=_min_posts(cfg, kind),
        min_alive=int(cfg["min_alive"]),
        confidence=float(_opt(cfg, "retire_confidence")),
        min_sd=float(_opt(cfg, "min_log_sd")),
        max_retire=int(_opt(cfg, "max_retire_per_run")),
    )


def cmd_prune(conn, cfg: dict, obs: list[fitness.Observation], *, dry_run: bool) -> list[int]:
    """Prune writers, designers and formats alike; returns every retired id."""
    names = swarm_store.genome_names(conn)
    retired: list[int] = []
    for kind in _KEY:
        live = [g.id for g in swarm_store.live_genomes(conn, kind)]
        decision = _decide(cfg, kind, obs, live)
        if not decision.retire:
            log.info("prune: no %s to retire (%d live)", kind, len(live))
            continue
        for gid in decision.retire:
            reason = decision.reason[gid]
            label = f"{kind} {gid} {names.get(gid, '?')}"
            if dry_run:
                log.info("dry run: would retire %s: %s", label, reason)
            else:
                swarm_store.retire_genome(conn, gid, reason)
                log.info("retired %s: %s", label, reason)
        retired += decision.retire
    return retired


def _ranked(conn, cfg: dict, obs: list[fitness.Observation], kind: str) -> list[mutate.Parent]:
    """Live rows of `kind` as Parents: genomes with at least min_posts scored posts first,
    best mean log relative first among them; the thinly scored, then the unscored, last."""
    scores = {s.genome_id: s for s in fitness.genome_scores(obs, _KEY[kind])}
    bar = _min_posts(cfg, kind)

    def rank(p: mutate.Parent) -> tuple:
        s = scores.get(p.genome.id)
        m = s.mean_log if s is not None else None
        return (m is None, p.n < bar, -(m or 0.0))

    parents = []
    for g in swarm_store.live_genomes(conn, kind):
        s = scores.get(g.id)
        threads = swarm_store.fetch_winning_threads(conn, g.id) if kind == "writer" else []
        parents.append(
            mutate.Parent(
                genome=g,
                median_relative=s.median_relative if s else None,
                n=s.n if s else 0,
                threads=threads,
            )
        )
    parents.sort(key=rank)
    return parents


def cmd_breed(
    conn,
    cfg: dict,
    obs: list[fitness.Observation],
    *,
    dry_run: bool,
    force: bool,
    call=None,
    rng: random.Random | None = None,
) -> list[Genome | Designer | FormatGenome]:
    """Fill the population back up to `population_size` per kind (`designer_population_size`
    for designers, `format_population_size`
    for formats), one child per gap."""
    rng = rng or random.Random()
    model = str(cfg.get("mutation_model") or "").strip() or drafting_model()
    born: list[Genome | Designer | FormatGenome] = []
    for kind in _KEY:
        size = _population_size(cfg, kind)
        parents = _ranked(conn, cfg, obs, kind)
        gap = size - len(parents)
        if gap <= 0:
            log.info("breed: %d live %ss, population is full", len(parents), kind)
            continue
        if not parents:
            log.warning("breed: no live %s to breed from", kind)
            continue
        bar = _min_posts(cfg, kind)
        scored = [p for p in parents if p.median_relative is not None and p.n >= bar]
        if not scored and not force:
            log.info(
                "breed: no %s with %d scored posts yet; nothing bred (use --force to breed anyway)",
                kind,
                bar,
            )
            continue
        pool = scored or parents
        taken = swarm_store.all_names(conn, kind)
        for i in range(gap):
            target = pool[i % len(pool)]
            try:
                if kind == "writer":
                    kwargs = {"call": call} if call is not None else {}
                    child = mutate.breed_writer(
                        parents, target, taken, model=model, kpi=str(_opt(cfg, "kpi")), **kwargs
                    )
                elif kind == "format":
                    child = mutate.breed_format(target.genome, taken, rng)
                else:
                    child = mutate.breed_designer(target.genome, taken, rng)
            except Exception:  # an invalid child or an API error: the gap stays for next run
                log.exception("breed: %s child of %s failed", kind, target.genome.name)
                continue
            taken.add(child.name)
            log.info(
                "%s %s child %r of %r: %s",
                "dry run:" if dry_run else "bred",
                kind,
                child.name,
                target.genome.name,
                child.notes,
            )
            if not dry_run:
                swarm_store.insert_genome(conn, child, kind=kind)
            born.append(child)
    return born


def _table(conn, obs: list[fitness.Observation], kind: str) -> list[str]:
    names = swarm_store.genome_names(conn)
    rows = swarm_store.list_genomes(conn)
    parents = {int(r["id"]): r["parent_id"] for r in rows}
    of_kind = {int(r["id"]) for r in rows if r["kind"] == kind}
    live = {g.id for g in swarm_store.live_genomes(conn, kind)}
    lines = [
        f"{kind + 's':<10} {'name':<22} {'state':<8} {'posts':>5} {'median rel':>10} "
        f"{'mean log':>8}  parent"
    ]
    scores = {s.genome_id: s for s in fitness.genome_scores(obs, _KEY[kind])}
    for gid in sorted(of_kind):
        s = scores.get(gid)
        n = s.n if s else 0
        med = f"{s.median_relative:.2f}" if s and s.median_relative is not None else "-"
        mlog = f"{s.mean_log:+.2f}" if s and s.mean_log is not None else "-"
        state = "live" if gid in live else "retired"
        parent = names.get(parents[gid], "-") if parents.get(gid) else "-"
        lines.append(
            f"{'':<10} {names.get(gid, '?'):<22} {state:<8} {n:>5} {med:>10} {mlog:>8}  {parent}"
        )
    return lines


def render_report(conn, cfg: dict, obs: list[fitness.Observation]) -> str:
    head = f"Swarm fitness on {cfg['kpi']}"
    horizon = float(_opt(cfg, "horizon_hours") or 0)
    at = f"at {horizon:g}h after posting" if horizon > 0 else "on the newest snapshot"
    rule = str(_opt(cfg, "prune_rule"))
    confidence = float(_opt(cfg, "retire_confidence"))
    gate = f" (retire at P(worse than the rest) >= 1 - {1 - confidence:.2f}/k)"
    lines = [
        f"{head} {at} (relative to the trailing {cfg['baseline_days']}-day median)",
        f"Prune rule: {rule}" + (gate if rule != "median" else ""),
    ]
    scored = [o for o in obs if o.relative is not None]
    credited = sum(o.genome_id is not None for o in scored)
    lines.append(
        f"Posts scored: {len(scored)}; credited to a writer genome: {credited} (the rest "
        "posted the control's text or were single posts)"
    )
    lines.append("")
    lines += _table(conn, obs, "writer")
    lines.append("")
    lines += _table(conn, obs, "designer")
    lines.append("")
    lines += _table(conn, obs, "format")
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
    ap.add_argument(
        "command", nargs="?", choices=["score", "prune", "breed", "report"], default=None
    )
    ap.add_argument("--kpi", default=None, help="override evolve.kpi in swarm/config.yaml")
    ap.add_argument("--baseline-days", type=float, default=None)
    ap.add_argument("--min-posts", type=int, default=None)
    ap.add_argument(
        "--dry-run", action="store_true", help="prune/breed: print, retire and store nothing"
    )
    ap.add_argument(
        "--force",
        action="store_true",
        help="breed even when no genome has min_posts scored posts yet",
    )
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
        if args.command in (None, "breed"):
            cmd_breed(conn, cfg, obs, dry_run=args.dry_run, force=args.force)
        if args.command in (None, "report"):
            print(render_report(conn, cfg, obs))
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
