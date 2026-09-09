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
"approve anyway". Nothing here changes a draft.
"""

from __future__ import annotations

import argparse
import logging
import sys

from dotenv import load_dotenv

from verify import store
from verify.settings import load_verify_config
from verify.verifier import trusted_hosts, verify_claim

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
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
