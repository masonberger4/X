"""Step 2b: verify the claims on pending drafts against the web and attach the evidence.

Usage:
  python run_verify.py                 # every pending draft with unchecked claims
  python run_verify.py --limit 3       # at most 3 drafts this run
  python run_verify.py --draft 12      # one draft
  python run_verify.py --redo          # check again, replacing earlier verdicts
  python run_verify.py --dry-run       # list what would be checked, no calls

Each claim becomes one web-enabled Claude call. Results land in claim_checks and appear
beside the claim in the approval queue with the source link and quoted sentence. A
contradicted claim blocks the Approve button until the human edits the draft or ticks
"approve anyway". Nothing here changes a draft's text.

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

from approval_queue import images
from approval_queue import store as queue_store
from verify import store, tables
from verify.settings import load_verify_config
from verify.verifier import ClaimCheck, trusted_hosts, verify_claim

log = logging.getLogger("run_verify")


def _root_config() -> dict:
    try:
        from config import load_config

        return load_config()
    except Exception:  # root config missing or unreadable
        return {}


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--limit", type=int, default=None, help="max drafts this run")
    ap.add_argument("--draft", type=int, default=None, help="only this draft id")
    ap.add_argument("--redo", action="store_true", help="re-check claims already checked")
    ap.add_argument("--dry-run", action="store_true", help="list claims, make no calls")
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
            for i in idxs:
                claim = d.draft.claims_to_verify[i]
                log.info("  claim %d [%s]: %s", i, claim.confidence, claim.claim[:100])
                if args.dry_run:
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
        log.info("done: %d claims checked, %d errors", checked, errors)
        if cfg["tables"].get("enabled", True):
            verify_tables(conn, args, cfg=cfg, root_cfg=root_cfg, hosts=hosts)
    finally:
        conn.close()
    return 0


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
        decision = _decide(conn, d, table, ratio=ratio, max_cells=max_cells)
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
        decision = _decide(conn, d, table, ratio=ratio, max_cells=max_cells)
        if decision.status == tables.PENDING:
            log.info("  table still has %d unchecked cell(s)", len(decision.unchecked))
        elif decision.status == tables.DROP:
            log.warning("  table dropped: %s", decision.reason)
            queue_store.drop_table(conn, d.id, decision.reason)
        else:
            drawn = tables.apply_row_drops(table, decision.blanked)
            path = images.attach_table(
                conn,
                d.id,
                drawn,
                source_url=d.url or "",
                blanked=_reindex(table, decision.blanked),
            )
            log.info("  table rendered to %s (%d cell(s) blanked)", path, len(decision.blanked))


def _decide(conn, d, table, *, ratio, max_cells):
    verdicts = [
        tables.CellVerdict(k.row, k.col, k.verdict, k.trusted)
        for k in store.table_checks_for_draft(conn, d.id)
    ]
    return tables.decide(table, verdicts, min_supported_ratio=ratio, max_cells=max_cells)


def _reindex(table, blanked: frozenset[tuple[int, int]]) -> frozenset[tuple[int, int]]:
    """Blanked positions in the drawn table, whose rows with a blanked label are gone."""
    keep = [r for r in range(len(table.rows)) if (r, 0) not in blanked]
    new_row = {r: i for i, r in enumerate(keep)}
    return frozenset((new_row[r], c) for r, c in blanked if r in new_row)


if __name__ == "__main__":
    sys.exit(main())
