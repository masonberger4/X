"""CLI: draft every scored candidate above threshold that has no draft yet.

Usage: python run_draft.py [--min-score 30] [--since-hours 48] [--limit N] [--dry-run]
                           [--no-examples] [--no-swarm] [--retry-failed] [--retag]

Drafts that pass every hard rule are stored as pending. Drafts the model could not get
past the hard rules are stored as status=failed with the reason, so they are not retried
on the next run and the reviewer can see why.

Every draft is a 3-6 post thread with exactly one visual, a chart or a table (the drafter
retries a draft that has neither, or whose chart holds a number the source does not). A
chart is rendered here to <db folder>/images/draft_<id>.png, unless images.enabled is false
in draft/config.yaml or matplotlib is missing (then the draft is stored without a picture);
a table waits for run_verify.py to check its cells before it is drawn.

Step 7: unless --no-examples (or examples.enabled: false in draft/config.yaml), recent human
edits and rejections from the approval queue are built ONCE per run into an examples block
that goes into every draft's system prompt; draft_examples records which decisions each
draft was shown.

Step 9: unless --no-swarm (or enabled: false in swarm/config.yaml), each story is also
written by the swarm (many cheap calls, one post per slot, see swarm/) and, when
control.enabled, by the single strong drafter as before; a jury of cheap judges picks the
winner, which is stored as the ordinary pending draft. swarm_runs / swarm_variants record
both versions and the verdict. A swarm that fails its hard rules loses to the control.
Phase four: each story also draws a FORMAT genome (thread, single or long post; how many
pictures and on which posts), which both the swarm and the control write to.
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import UTC, datetime, timedelta

from dotenv import load_dotenv

from approval_queue import images, store
from draft.chart import Style
from draft.drafter import DraftRejected, draft_item, revise_item, story_handles
from draft.examples import (
    EditExample,
    RejectionExample,
    format_examples_block,
    select_edit_examples,
    select_rejections,
)
from draft.schema import Draft
from draft.settings import load_draft_config
from draft.tags import tag_problems
from swarm import engine as swarm_engine
from swarm import store as swarm_store
from swarm.prompts import Brief
from swarm.settings import load_swarm_config
from verify import store as verify_store

log = logging.getLogger("run_draft")


def build_examples(
    conn: store.sqlite3.Connection, cfg: dict
) -> tuple[str | None, list[EditExample], list[RejectionExample]]:
    """Select recent edits/rejections and format the block. Built once per run."""
    ex_cfg = cfg.get("examples") or {}
    now = datetime.now(UTC)
    lookback = float(ex_cfg.get("lookback_days", 60))
    rows = store.fetch_decisions_for_voice(conn, now - timedelta(days=lookback))
    edits = select_edit_examples(rows, ex_cfg, now=now)
    rejections = select_rejections(rows, ex_cfg, now=now)
    block = format_examples_block(edits, rejections)
    log.info(
        "voice examples: %d edits, %d rejections (last %d days)",
        len(edits),
        len(rejections),
        lookback,
    )
    log.debug(
        "example decision ids: edits=%s rejections=%s",
        [e.decision_id for e in edits],
        [r.decision_id for r in rejections],
    )
    return block, edits, rejections


def draft_with_swarm(
    conn: store.sqlite3.Connection,
    c: store.Candidate,
    swarm_cfg: dict,
    examples_block: str | None,
) -> tuple[object, int]:
    """Step 9: swarm + control, jury, winner. Returns (DraftResult or DraftRejected, run id).

    The control is draft_item exactly as the pre-step-9 path; the swarm is
    swarm.engine.run_swarm. Both variants are recorded; the run row gets its draft_id once
    the winner is stored. The run also picks the designer (phase three) whose Style the
    winner's picture starts from; `designer_style_for(conn, run_id)` returns it."""
    handles = story_handles(source_text=f"{c.title}\n{c.abstract}", url=c.url, source=c.source)
    brief = Brief(
        title=c.title,
        abstract=c.abstract,
        url=c.url,
        source=c.source,
        published_at=c.published_at,
        suggested_angle=c.suggested_angle,
        rationale=c.rationale,
        handles=tuple(handles),
    )
    genome = swarm_store.next_genome(conn)
    designer = swarm_store.next_designer(conn)
    format_genome = swarm_store.next_format(conn)
    long_max = int((swarm_cfg.get("formats") or {}).get("long_max_chars", 4000))
    fmt = format_genome.to_format(long_max)
    log.info(
        "%s: format %s (%s, %d visual(s)), genome %s, designer %s",
        c.item_id,
        format_genome.name,
        fmt.shape,
        fmt.visuals,
        genome.name,
        designer.name,
    )
    log_rows: dict = {"format": fmt.to_dict()}
    swarm_result = None
    swarm_problem: str | None = None
    try:
        swarm_result = swarm_engine.run_swarm(
            brief, genome, swarm_cfg, examples_block=examples_block, fmt=fmt
        )
        log_rows["swarm"] = swarm_result.log
    except swarm_engine.SwarmFailed as exc:
        swarm_problem = str(exc)
        log.warning("%s: swarm failed: %s", c.item_id, exc)
    except Exception as exc:  # API failure inside the swarm: the control still runs
        swarm_problem = f"error: {exc}"
        log.exception("%s: swarm error", c.item_id)

    control_result = None
    control_problem: list[str] | None = None
    if (swarm_cfg.get("control") or {}).get("enabled", True):
        try:
            control_result = draft_item(
                title=c.title,
                abstract=c.abstract,
                url=c.url,
                source=c.source,
                published_at=c.published_at,
                suggested_angle=c.suggested_angle,
                rationale=c.rationale,
                examples_block=examples_block,
                fmt=fmt,
                handles=handles,
            )
        except DraftRejected as exc:
            control_problem = exc.reasons
        # any other exception propagates: the caller retries the story next run

    calls = swarm_result.calls if swarm_result else 0
    winner: str | None
    verdict = None
    if swarm_result and control_result:
        verdict = swarm_engine.compare(
            swarm_result.draft_result.draft, control_result.draft, brief, swarm_cfg
        )
        calls += verdict.calls
        log_rows["jury"] = verdict.votes
        winner = verdict.winner
    elif swarm_result:
        winner = "swarm"
    elif control_result:
        winner = "control"
    else:
        winner = None
    run_id = swarm_store.record_run(
        conn,
        item_id=c.item_id,
        cluster_id=c.cluster_id,
        genome_id=genome.id,
        winner=winner,
        calls=calls,
        log=log_rows,
        designer_id=designer.id,
        format_id=format_genome.id,
    )
    swarm_store.record_variant(
        conn,
        run_id,
        role="swarm",
        model=swarm_result.draft_result.model if swarm_result else None,
        draft=swarm_result.draft_result.draft if swarm_result else None,
        problems=[swarm_problem] if swarm_problem else None,
    )
    if (swarm_cfg.get("control") or {}).get("enabled", True):
        swarm_store.record_variant(
            conn,
            run_id,
            role="control",
            model=control_result.model if control_result else None,
            draft=control_result.draft if control_result else None,
            problems=control_problem,
        )
    log.info(
        "%s: swarm %s, control %s -> %s (%d calls)",
        c.item_id,
        "ok" if swarm_result else "failed",
        "ok" if control_result else ("off" if control_problem is None else "failed"),
        winner,
        calls,
    )
    if winner == "swarm":
        return swarm_result.draft_result, run_id
    if winner == "control":
        return control_result, run_id
    reasons = list(control_problem or [])
    if swarm_problem:
        reasons.append(f"swarm: {swarm_problem}")
    return DraftRejected(reasons or ["no variant produced a draft"]), run_id


def designer_style_for(conn: store.sqlite3.Connection, run_id: int | None) -> Style | None:
    """The Style preset of the designer recorded on a swarm run; None without one."""
    if run_id is None:
        return None
    row = conn.execute("SELECT designer_id FROM swarm_runs WHERE id = ?", (run_id,)).fetchone()
    if not row or row[0] is None:
        return None
    designer = swarm_store.get_genome(conn, int(row[0]))
    if designer is None or not hasattr(designer, "style"):
        return None
    return Style().apply(designer.style)


RETAG_NOTE = "apply mentions and hashtags (hard rule 11)"
RETAG_INSTRUCTIONS = (
    "Apply HARD RULE 11 to every post: write each listed account as its @handle where the "
    "post names it, and write every formal drug name and named trial as a hashtag, spelled "
    "as the source spells it. Change nothing else: keep every sentence, number, claim and "
    "the visual exactly as they are."
)


def retag_problems(row: store.DraftRow) -> list[str]:
    """Rule 11 violations in a stored draft's current posts (the handles are looked up
    from config.yaml for its story). Empty when the draft already complies."""
    handles = story_handles(
        source_text=f"{row.title}\n{row.abstract}", url=row.url, source=row.source
    )
    out: list[str] = []
    for i, post in enumerate(row.draft.all_posts()):
        out += [f"thread[{i}] {p}" for p in tag_problems(post, handles)]
    return out


def retag_drafts(conn: store.sqlite3.Connection, *, dry_run: bool = False) -> int:
    """`--retag`: bring every current pending and approved draft (not yet posted) under
    hard rule 11 by revising it through the queue's own path, `revise_item` with the
    instruction to change only the tags, `store.revise` (status unchanged, a `revise`
    decision with note RETAG_NOTE) and `carry_over_checks`. Drafts that already comply are
    skipped; a draft whose revision fails is left as it was. Returns the number revised."""
    rows = store.list_drafts(conn, store.STATUS_PENDING) + store.list_drafts(
        conn, store.STATUS_APPROVED
    )
    posted = store.publish_states(conn, [r.id for r in rows])
    todo = []
    for r in rows:
        if r.id in posted:
            continue
        problems = retag_problems(r)
        if problems:
            todo.append((r, problems))
    log.info("retag: %d of %d current draft(s) break rule 11", len(todo), len(rows))
    done = 0
    for row, problems in todo:
        log.info("draft %d (%s): %s", row.id, row.status, "; ".join(problems))
        if dry_run:
            continue
        try:
            result = revise_item(
                current=row.draft,
                instructions=RETAG_INSTRUCTIONS,
                title=row.title,
                abstract=row.abstract,
                url=row.url,
                source=row.source,
                suggested_angle=row.suggested_angle or None,
                rationale=row.rationale or None,
            )
        except DraftRejected as exc:
            log.warning("draft %d: retag broke a hard rule, left as it was: %s", row.id, exc)
            continue
        except Exception as exc:  # API / network errors: keep the draft as it was
            log.error("draft %d: retag failed, left as it was: %s", row.id, exc)
            continue
        store.revise(conn, row.id, draft=result.draft, model=result.model, note=RETAG_NOTE)
        verify_store.carry_over_checks(
            conn, row.id, [c.claim for c in result.draft.claims_to_verify]
        )
        verify_store.carry_over_table_checks(conn, row.id, row.draft.table, result.draft.table)
        images.attach_chart(conn, row.id, result.draft.chart, source_url=row.url)
        if result.draft.extra_visuals:
            images.attach_extra_charts(conn, row.id, result.draft.extra_visuals, source_url=row.url)
        done += 1
        log.info("draft %d retagged (%d attempt(s))", row.id, result.attempts)
    return done


def _default_min_score() -> float:
    """config.yaml scoring.threshold (the digest's bar), else 30 on the 0-50 scale."""
    try:
        import config as root_config

        return float((root_config.load_config().get("scoring") or {}).get("threshold", 30))
    except Exception:  # root config missing or unreadable
        return 30.0


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument(
        "--min-score",
        type=float,
        default=None,
        help="score total (0-50) a story needs to be drafted; default: config.yaml "
        "scoring.threshold",
    )
    ap.add_argument("--since-hours", type=float, default=48.0)
    ap.add_argument("--limit", type=int, default=10, help="max items to draft this run")
    ap.add_argument("--dry-run", action="store_true", help="list candidates, do not call the API")
    ap.add_argument(
        "--no-examples",
        action="store_true",
        help="do not add recent human edits/rejections to the prompt (step 7)",
    )
    ap.add_argument(
        "--no-swarm",
        action="store_true",
        help="draft with the single strong model only, as before step 9 (no swarm, no jury)",
    )
    ap.add_argument(
        "--retry-failed",
        action="store_true",
        help="also draft stories whose only drafts failed the hard rules",
    )
    ap.add_argument(
        "--retag",
        action="store_true",
        help="revise every current pending/approved draft that breaks the mention/hashtag "
        "rule (hard rule 11) instead of drafting new stories; --dry-run lists them",
    )
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    if args.min_score is None:
        args.min_score = _default_min_score()

    conn = store.connect()
    try:
        if args.retag:
            n = retag_drafts(conn, dry_run=args.dry_run)
            log.info("retag: %d draft(s) revised", n)
            return 0
        if not store.step1_tables_present(conn):
            log.error(
                "no items/clusters/scores tables in %s; run step 1 (run_ingest/run_score) first",
                store.db_path(),
            )
            return 1
        draft_cfg = load_draft_config()
        examples_block: str | None = None
        edits: list[EditExample] = []
        rejections: list[RejectionExample] = []
        if args.no_examples:
            log.info("voice examples: disabled by --no-examples")
        elif not (draft_cfg.get("examples") or {}).get("enabled", True):
            log.info("voice examples: disabled in draft/config.yaml")
        else:
            examples_block, edits, rejections = build_examples(conn, draft_cfg)
        swarm_cfg = load_swarm_config()
        use_swarm = bool(swarm_cfg.get("enabled", True)) and not args.no_swarm
        if args.no_swarm:
            log.info("swarm: disabled by --no-swarm")
        elif not use_swarm:
            log.info("swarm: disabled in swarm/config.yaml")
        else:
            swarm_store.ensure_tables(conn)
            log.info(
                "swarm: on (%s, fan_out %s, layers %s, control %s)",
                swarm_cfg.get("model"),
                swarm_cfg.get("fan_out"),
                swarm_cfg.get("layers"),
                "on" if (swarm_cfg.get("control") or {}).get("enabled", True) else "off",
            )
        edit_ids = [e.decision_id for e in edits]
        rejection_ids = [r.decision_id for r in rejections]
        if args.dry_run:
            log.info(
                "dry run: examples block is %d chars; edit decisions %s; rejection decisions %s",
                len(examples_block or ""),
                edit_ids,
                rejection_ids,
            )
        candidates = store.fetch_candidates(args.min_score, args.since_hours, conn=conn)
        todo = [
            c
            for c in candidates
            if not store.has_draft(conn, c.item_id, c.cluster_id, ignore_failed=args.retry_failed)
        ][: args.limit]
        log.info(
            "%d candidates >= %.1f in last %.0fh, %d without a draft",
            len(candidates),
            args.min_score,
            args.since_hours,
            len(todo),
        )
        drafted = failed = charts = 0
        for c in todo:
            log.info("%s %.1f %s", c.source, c.total, c.title[:80])
            if args.dry_run:
                continue
            if args.retry_failed:
                store.delete_failed_drafts(conn, c.item_id, c.cluster_id)
            run_id: int | None = None
            try:
                if use_swarm:
                    result, run_id = draft_with_swarm(conn, c, swarm_cfg, examples_block)
                    if isinstance(result, DraftRejected):
                        raise result
                else:
                    result = draft_item(
                        title=c.title,
                        abstract=c.abstract,
                        url=c.url,
                        source=c.source,
                        published_at=c.published_at,
                        suggested_angle=c.suggested_angle,
                        rationale=c.rationale,
                        examples_block=examples_block,
                    )
            except DraftRejected as exc:
                failed += 1
                draft_id = store.insert_draft(
                    conn,
                    item_id=c.item_id,
                    cluster_id=c.cluster_id,
                    model=exc.__class__.__name__,
                    draft=Draft([], "", ""),
                    status=store.STATUS_FAILED,
                    rejection_reason="; ".join(exc.reasons),
                )
                store.record_examples(conn, draft_id, edit_ids, rejection_ids)
                if run_id is not None:
                    swarm_store.set_run_draft(conn, run_id, draft_id)
                continue
            except Exception:
                log.exception("API failure drafting %s; will retry next run", c.item_id)
                continue
            draft_id = store.insert_draft(
                conn,
                item_id=c.item_id,
                cluster_id=c.cluster_id,
                model=result.model,
                draft=result.draft,
            )
            store.record_examples(conn, draft_id, edit_ids, rejection_ids)
            if run_id is not None:
                swarm_store.set_run_draft(conn, run_id, draft_id)
            drafted += 1
            if result.flagged_numbers:
                log.warning("%s: numbers flagged for review: %s", c.item_id, result.flagged_numbers)
            style = designer_style_for(conn, run_id)
            if images.attach_chart(
                conn, draft_id, result.draft.chart, source_url=c.url, cfg=draft_cfg, style=style
            ):
                charts += 1
            if result.draft.extra_visuals:
                images.attach_extra_charts(
                    conn,
                    draft_id,
                    result.draft.extra_visuals,
                    source_url=c.url,
                    cfg=draft_cfg,
                    style=style,
                )
        log.info(
            "done: %d drafted (%d with a chart), %d failed hard rules", drafted, charts, failed
        )
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
