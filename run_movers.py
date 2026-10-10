"""Market movers: biotech stocks that moved 5% or more, and whether a story is behind them.

Usage:
  python run_movers.py                 # the automatic run: screen when it is due
                                       # (movers/config.yaml every_hours, not_before)
  python run_movers.py --now           # screen now, whatever the time since the last
  python run_movers.py --dry-run       # screen and print the story check's prompt;
                                       # no call, nothing recorded, nothing queued

The screen is plain code: daily and intraday prices for every ticker in the universe (the
root config.yaml's companies with a ticker plus movers/config.yaml `extra_tickers`) from
Yahoo Finance's free chart API, each compared with the session before. A move of
`threshold_pct` or more, up or down, in the last completed session, after that close, or
in this morning's pre-market, passes. Only moves no earlier screen recorded are new; the
new ones (the biggest `check.max_checked`) go to ONE Claude call with web search that says
whether a story is behind each move and whether it is worth a piece. Those worth a piece
go on the studio's radar (/studio/radar) and the best `auto_queue` are queued for the
studio, which picks the angle and voice and writes the piece. Nothing here posts.
"""

from __future__ import annotations

import argparse
import logging
import sys

from dotenv import load_dotenv


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0], allow_abbrev=False)
    ap.add_argument("--now", action="store_true", help="screen now, whatever the time")
    ap.add_argument(
        "--dry-run", action="store_true", help="screen and print the check's prompt only"
    )
    ap.add_argument("-v", "--verbose", action="store_true")
    return ap.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    args = _parse_args(argv)
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="backslashreplace")
        except (AttributeError, ValueError):
            pass
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    from movers import runner

    return runner.run(force=args.now, dry_run=args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
