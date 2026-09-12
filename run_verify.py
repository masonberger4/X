"""Step 2b: verify the claims on pending drafts against the web and attach the evidence.

Usage:
  python run_verify.py                 # every pending draft with unchecked claims
  python run_verify.py --limit 3       # at most 3 drafts this run
  python run_verify.py --draft 12      # one draft
  python run_verify.py --redo          # check again, replacing earlier verdicts
  python run_verify.py --dry-run       # list what would be checked, no calls
  python run_verify.py --auto-revise   # then revise drafts with failed claims and re-check
  python run_verify.py --no-auto-revise  # one run without the loop, whatever the config says

Each claim becomes one web-enabled Claude call. Results land in claim_checks and appear
beside the claim in the approval queue with the source link and quoted sentence. A
contradicted claim blocks the Approve button until the human edits the draft or ticks
"approve anyway". Nothing here changes a draft's text, except through the verify-revise
loop below.

With `--auto-revise` (or `auto_revise: enabled: true` in verify/config.yaml) a draft that
still has a contradicted or unverified claim after the pass goes back through the drafter
with those claims, exactly as the queue's Revise button with an empty box does, its
supported verdicts are kept, the new or changed claims are checked, and the round repeats
until every claim is supported or `max_rounds` (per run) / `max_rounds_per_draft` (for
life) is hit. See verify/autorevise.py. Tables are not part of the loop.

A draft with a comparison table gets a second pass: every cell that is not verbatim in the
source article is one more web call (table_checks); once every cell has a verdict the
table is drawn with unsupported cells blanked (approval_queue.images.attach_table), or
dropped when a cell is contradicted or too few pass (verify/tables.py). The same --limit,
--draft, --redo and --dry-run apply; `tables: enabled: false` in verify/config.yaml skips
the pass.
"""

from __future__ import annotations

import argparse
import logging
import sys

from dotenv import load_dotenv

from approval_queue import store as queue_store
from verify import autorevise, render, store, tables
from verify.settings import load_verify_config
from verify.verifier import ClaimCheck, trusted_hosts, verify_claim

log = logging.getLogger("run_verify")


def _root_config() -> dict:
    return render.root_config()


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--limit", type=int, default=None, help="max drafts this run")
    ap.add_argument("--draft", type=int, default=None, help="only this draft id")
    ap.add_argument("--redo", action="store_true", help="re-check claims already checked")
    ap.add_argument("--dry-run", action="store_true", help="list claims, make no calls")
    ap.add_argument(
        "--auto-revise",
        dest="auto_revise",
        action="store_true",
        default=None,
        help="revise drafts with failed claims and check again (verify/config.yaml auto_revise)",
    )
    ap.add_argument(
        "--no-auto-revise",
        dest="auto_revise",
        action="store_false",
        help="skip the verify-revise loop this run",
    )
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    cfg = load_verify_config()
    root_cfg = _root_config()
    hosts = trusted_hosts(cfg, root_cfg)
    max_claims = int(cfg.get("max_claims_per_draft", 6))

    conn = store.connect()
    try:
        drafts = store.pending_drafts_with_claims(conn)
        if args.draft is not None:
            drafts = [d for d in drafts if d.id == args.draft]
        todo = []
        for d in drafts:
            if args.redo:
                idxs = list(range(len(d.draft.claims_to_verify)))
            else:
                idxs = store.unchecked_indexes(conn, d)
            idxs = [i for i in idxs if i < max_claims]
            if idxs:
                todo.append((d, idxs))
        if args.limit is not None:
            todo = todo[: args.limit]
        log.info(
            "%d pending drafts with claims, %d to check (%d claims) with %s",
            len(drafts),
            len(todo),
            sum(len(i) for _, i in todo),
            cfg.get("model"),
        )
        checked = errors = 0
        for d, idxs in todo:
            log.info("draft %d: %s", d.id, (d.title or d.item_id)[:80])
            if args.redo and not args.dry_run:
                store.delete_checks(conn, d.id)
            c, e = check_claims(
                conn, d, idxs, dry_run=args.dry_run, cfg=cfg, root_cfg=root_cfg, hosts=hosts
            )
            checked += c
            errors += e
        log.info("done: %d claims checked, %d errors", checked, errors)
        auto = cfg["auto_revise"]
        enabled = auto.get("enabled", False) if args.auto_revise is None else args.auto_revise
        if enabled and not args.dry_run:
            drafts = store.pending_drafts_with_claims(conn)
            if args.draft is not None:
                drafts = [d for d in drafts if d.id == args.draft]
            if args.limit is not None:
                drafts = drafts[: args.limit]
            for d in drafts:
                auto_revise(
                    conn,
                    d.id,
                    max_rounds=int(auto.get("max_rounds", 3)),
                    lifetime_cap=int(auto.get("max_rounds_per_draft", 6)),
                    max_claims=max_claims,
                    cfg=cfg,
                    root_cfg=root_cfg,
                    hosts=hosts,
                )
        if cfg["tables"].get("enabled", True):
            verify_tables(conn, args, cfg=cfg, root_cfg=root_cfg, hosts=hosts)
    finally:
        conn.close()
    return 0


def check_claims(
    conn, d, idxs: list[int], *, dry_run: bool, cfg: dict, root_cfg: dict, hosts: set[str]
) -> tuple[int, int]:
    """Send the given claims of one draft to the verifier and store each verdict. Returns
    (checked, errors); one bad claim never stops the run."""
    checked = errors = 0
    for i in idxs:
        claim = d.draft.claims_to_verify[i]
        log.info("  claim %d [%s]: %s", i, claim.confidence, claim.claim[:100])
        if dry_run:
            continue
        try:
            check = verify_claim(
                i,
                claim.claim,
                title=d.title or "",
                url=d.url or "",
                single_post=d.draft.single_post,
                published_at=None,
                cfg=cfg,
                root_cfg=root_cfg,
                hosts=hosts,
            )
        except Exception as exc:  # one bad claim never stops the run
            errors += 1
            log.warning("  claim %d failed: %s", i, exc)
            continue
        store.insert_check(conn, d.id, check, str(cfg.get("model")))
        checked += 1
        log.info(
            "  -> %s%s %s",
            check.verdict,
            "" if check.trusted or check.verdict == "unverified" else " (untrusted)",
            check.source_url,
        )
    return checked, errors


def auto_revise(
    conn,
    draft_id: int,
    *,
    max_rounds: int,
    lifetime_cap: int,
    max_claims: int,
    cfg: dict,
    root_cfg: dict,
    hosts: set[str],
) -> int:
    """The verify-revise loop for one draft (verify/autorevise.py). Returns the number of
    revisions made. A claim left unchecked (a failed web call, or past max_claims) stops the
    loop: the drafter is only ever handed verdicts, never guesses."""
    rounds = 0
    for _ in range(max_rounds):
        d = queue_store.get_draft(conn, draft_id)
        if d is None:
            return rounds
        if [i for i in store.unchecked_indexes(conn, d) if i < max_claims]:
            log.info("draft %d: unchecked claims remain, not revising", draft_id)
            return rounds
        res = autorevise.revise_round(conn, draft_id, lifetime_cap=lifetime_cap)
        if not res.revised:
            if res.problems:
                log.warning(
                    "draft %d: %d claim problem(s) left, %s", draft_id, res.problems, res.reason
                )
            else:
                log.info("draft %d: %s", draft_id, res.reason)
            return rounds
        rounds += 1
        log.info(
            "draft %d: auto-revised (round %d, %d problem(s), %d verdict(s) kept)",
            draft_id,
            rounds,
            res.problems,
            res.kept,
        )
        d = queue_store.get_draft(conn, draft_id)
        idxs = [i for i in store.unchecked_indexes(conn, d) if i < max_claims]
        check_claims(conn, d, idxs, dry_run=False, cfg=cfg, root_cfg=root_cfg, hosts=hosts)
    d = queue_store.get_draft(conn, draft_id)
    left = autorevise.claim_problems(store.checks_for_draft(conn, draft_id)) if d else []
    if left:
        log.warning(
            "draft %d: %d claim problem(s) left after %d round(s) this run",
            draft_id,
            len(left),
            rounds,
        )
    return rounds


def verify_tables(conn, args, *, cfg: dict, root_cfg: dict, hosts: set[str]) -> None:
    """The table pass: fill in every cell verdict, then draw or drop each table."""
    tcfg = cfg["tables"]
    max_cells = int(tcfg.get("max_cells_per_draft", 30))
    ratio = float(tcfg.get("min_supported_ratio", 0.6))
    drafts = store.pending_drafts_with_tables(conn)
    if args.draft is not None:
        drafts = [d for d in drafts if d.id == args.draft]
    if args.limit is not None:
        drafts = drafts[: args.limit]
    if not drafts:
        return
    log.info("%d pending draft(s) with an unrendered table", len(drafts))
    for d in drafts:
        table = d.draft.table
        assert table is not None
        log.info("draft %d table: %s (%d cells)", d.id, table.title[:60], len(table.cells()))
        if args.redo and not args.dry_run:
            store.delete_table_checks(conn, d.id)
        if not args.dry_run:
            for r, c in tables.source_backed_cells(table, f"{d.title}\n{d.abstract}"):
                if (r, c) in {(k.row, k.col) for k in store.table_checks_for_draft(conn, d.id)}:
                    continue
                store.insert_table_check(
                    conn,
                    d.id,
                    r,
                    c,
                    table.rows[r][c],
                    ClaimCheck(
                        0,
                        table.rows[r][c],
                        "supported",
                        d.url or "",
                        table.rows[r][c],
                        "verbatim in the source article",
                        True,
                    ),
                    tables.SOURCE_MODEL,
                )
        decision = render.decide(conn, d, table, ratio=ratio, max_cells=max_cells, hosts=hosts)
        for r, c in decision.unchecked:
            claim = tables.cell_claim(table, r, c)
            log.info("  cell (%d,%d): %s", r, c, claim[:100])
            if args.dry_run:
                continue
            try:
                check = verify_claim(
                    0,
                    claim,
                    title=d.title or "",
                    url=d.url or "",
                    single_post=d.draft.single_post,
                    published_at=None,
                    cfg=cfg,
                    root_cfg=root_cfg,
                    hosts=hosts,
                )
            except Exception as exc:  # one bad cell never stops the run
                log.warning("  cell (%d,%d) failed: %s", r, c, exc)
                continue
            store.insert_table_check(conn, d.id, r, c, table.rows[r][c], check, str(cfg["model"]))
            log.info(
                "  -> %s%s %s",
                check.verdict,
                "" if check.trusted else " (untrusted)",
                check.source_url,
            )
        if args.dry_run:
            continue
        render.finalize_table(conn, d, cfg=cfg, hosts=hosts)


if __name__ == "__main__":
    sys.exit(main())
