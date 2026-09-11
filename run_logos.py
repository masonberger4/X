#!/usr/bin/env python
"""CLI: fill assets/logos/ with each configured company's own site icon.

For every company in config.yaml (`companies.feeds` and `branding.companies`) this fetches
the homepage of its site (`domain:`, else derived from the feed URL), picks the icon the
page advertises (apple-touch-icon first, then the largest favicon, then the well-known
paths) and saves it as `<branding.logos_dir>/<key>.png`, ready for draft/branding.py.
It is an operator command, never part of the pipeline: run it once, look at the folder,
delete what you do not like, and re-run with --force after a rebrand.

usage: python run_logos.py [--only KEY ...] [--force] [--dry-run] [-v]
  --only KEY   fetch only these company keys (repeatable)
  --force      overwrite a logo that already exists
  --dry-run    print which site and icon would be used, download nothing
  -v           debug logging
"""

from __future__ import annotations

import argparse
import logging
import sys
from typing import Any

from draft import logos as logomod
from ingest import http

log = logging.getLogger("run_logos")


def company_entries(cfg: dict[str, Any]) -> list[dict[str, Any]]:
    entries = [e for e in ((cfg.get("companies") or {}).get("feeds") or []) if isinstance(e, dict)]
    entries += [
        e for e in ((cfg.get("branding") or {}).get("companies") or []) if isinstance(e, dict)
    ]
    seen: set[str] = set()
    out = []
    for e in entries:
        key = str(e.get("key") or "").strip()
        if key and key not in seen:
            seen.add(key)
            out.append(e)
    return out


def fetch_logo(
    domain: str, *, user_agent: str | None, dry_run: bool = False
) -> tuple[bytes | None, str]:
    """The first icon candidate on `domain` that normalises to a PNG, and its URL ('' when
    none did). Every fetch goes through ingest.http."""
    base = f"https://{domain}/"
    try:
        html = http.get_text(base, user_agent=user_agent, max_attempts=2)
    except Exception as exc:
        log.warning("%s: homepage failed (%s); trying well-known paths", domain, exc)
        html = ""
    for cand in logomod.icon_candidates(html, base):
        if dry_run:
            return None, cand.url
        try:
            data, ctype = http.get_bytes(cand.url, user_agent=user_agent, max_attempts=1)
        except Exception as exc:
            log.debug("%s: %s failed (%s)", domain, cand.url, exc)
            continue
        png = logomod.normalise_png(data, ctype)
        if png:
            return png, cand.url
        log.debug("%s: %s is not a usable image", domain, cand.url)
    return None, ""


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--only", action="append", default=[], metavar="KEY")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )
    from config import load_config
    from panel.frozen import data_dir

    cfg = load_config()
    section = cfg.get("branding") or {}
    logos_dir = data_dir() / str(section.get("logos_dir") or logomod_default())
    user_agent = (cfg.get("http") or {}).get("user_agent")
    entries = company_entries(cfg)
    if args.only:
        wanted = set(args.only)
        entries = [e for e in entries if e.get("key") in wanted]
    fetched = skipped = missing = 0
    for e in entries:
        key = str(e["key"])
        target = logos_dir / f"{key}.png"
        if target.exists() and not args.force and not args.dry_run:
            log.info("%s: exists, skipped (--force to refetch)", key)
            skipped += 1
            continue
        domain = logomod.company_domain(e)
        if not domain:
            log.warning("%s: no site to look at; add `domain:` to its config entry", key)
            missing += 1
            continue
        png, url = fetch_logo(domain, user_agent=user_agent, dry_run=args.dry_run)
        if args.dry_run:
            log.info("%s: would fetch %s", key, url or f"(nothing found on {domain})")
            continue
        if png is None:
            log.warning("%s: no usable icon on %s; add the press-kit PNG by hand", key, domain)
            missing += 1
            continue
        logos_dir.mkdir(parents=True, exist_ok=True)
        target.write_bytes(png)
        log.info("%s: saved %s from %s", key, target, url)
        fetched += 1
    log.info(
        "done: %d saved, %d skipped, %d missing (review %s)", fetched, skipped, missing, logos_dir
    )
    return 0


def logomod_default() -> str:
    from draft.branding import DEFAULT_LOGOS_DIR

    return DEFAULT_LOGOS_DIR


if __name__ == "__main__":
    sys.exit(main())
