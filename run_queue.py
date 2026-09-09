"""CLI: serve the approval queue on localhost.

Usage: python run_queue.py [--port 8000] [--reload]
"""

from __future__ import annotations

import argparse
import logging

import uvicorn
from dotenv import load_dotenv


def main(argv: list[str] | None = None) -> None:
    load_dotenv()
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--host", default="127.0.0.1", help="bind address (localhost only by default)")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--reload", action="store_true")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO)
    uvicorn.run("approval_queue.app:app", host=args.host, port=args.port, reload=args.reload)


if __name__ == "__main__":
    main()
