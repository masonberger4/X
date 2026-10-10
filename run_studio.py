"""Studio: one long Opus session per post, from research to finished cards.

Usage:
  python run_studio.py                     # the automatic run: act on the editor's
                                           # requests, then start a new piece if the
                                           # limits in studio/config.yaml allow one
  python run_studio.py --now               # start a new piece now, whatever the limits
  python run_studio.py --resume-only       # only act on the editor's requests
  python run_studio.py --topic "next-gen CTLA-4"   # a piece on this topic, now
  python run_studio.py --story 123         # a piece from feed story (cluster) 123, now
  python run_studio.py --angle deal_decoder        # with --topic/--story/--now: this angle
  python run_studio.py --topic T --trial polish    # with --topic/--story: a side-by-side
                                           # trial of the usage savings (current |
                                           # polish | all; studio/settings.py TRIALS)
  python run_studio.py --checkpoint        # stop after research for the editor to read
  python run_studio.py --no-checkpoint     # write straight through after research
  python run_studio.py --list              # show recent pieces and queued topics
  python run_studio.py --dry-run           # print the first prompt, start nothing
  python run_studio.py --scan              # the radar's daily news scan, when it is due
  python run_studio.py --scan-now          # the scan now, whatever the time since the last
  python run_studio.py --scan --dry-run    # print the scan's prompt, run nothing
  python run_studio.py --learn             # measure the posted pieces against X and
                                           # rewrite the playbook when enough are new
  python run_studio.py --learn-now         # the same, rewriting the playbook now
  python run_studio.py --learn --dry-run   # print what X says and the rewrite prompt

Each piece is ONE Claude Code session (studio/config.yaml `model`, `effort`: Opus 5.5 at
max) run from the piece's own folder under `workspace_dir`. It researches the topic and
writes factbase.md; then (straight away, or after the editor presses Continue when the
piece stops at the research checkpoint) it writes the post, designs the cards as HTML and
runs its own cold fact-check through a sub-agent; the app draws the cards, counts
characters the way X does, runs the safety checks and hands any problems back to the same
session; the finished piece goes into the approval queue as a pending draft with its
cards attached. Nothing here posts. Requests from the studio page (Continue, Revise,
Resume) are picked up by the next run; `--resume-only` is the studio page's button.

Topics: a topic queued from the studio or radar page (or --topic/--story) comes first;
otherwise the session gets the radar: today's scan topics, the catalysts coming up and the
top scored stories of the last two days that no piece has used, and picks one, or finds a
better story with its own news scan.

The radar (studio/config.yaml `radar`): `--scan` (the `studio_scan` step, before `studio`
in every automatic run) runs one call with web search at most every `scan_every_hours`:
it proposes the next topics and reports dated catalysts (PDUFA dates, readouts,
conference slots) for the calendar on /studio/radar; `--scan-now` (the radar page's
button) runs it now. Each piece's research adds the catalysts it found.

Learning from X (studio/config.yaml `learn`): every brief carries what X says about the
earlier pieces (each one's first post, `horizon_hours` after posting, against the
account's median) and a lean among the angles, shapes and hooks on offer. `--learn` (the
`studio_learn` step, after the feedback snapshot) measures the posted pieces and, once
`rewrite_min_new` scored pieces are new to it, rewrites the playbook from the evidence and
the editor's hand edits in one call; `--learn-now` (the performance page's button) rewrites
whatever the counts. Every version is kept on /studio/performance, where any one can be
put back.
"""

from __future__ import annotations

import argparse
import logging
import sys

from dotenv import load_dotenv

log = logging.getLogger("run_studio")


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0], allow_abbrev=False)
    ap.add_argument("--now", action="store_true", help="start a new piece now, ignoring the limits")
    ap.add_argument("--resume-only", action="store_true", help="only act on the editor's requests")
    ap.add_argument("--topic", default="", help="start a piece on this topic now")
    ap.add_argument("--story", type=int, default=None, help="start a piece from this feed story id")
    ap.add_argument("--angle", default="", help="the angle for the new piece (studio/angles.yaml)")
    ap.add_argument(
        "--trial",
        default="",
        help="with --topic/--story: run the piece as a usage trial (current, polish, all)",
    )
    ap.add_argument(
        "--checkpoint",
        dest="checkpoint",
        action="store_true",
        default=None,
        help="stop after research for the editor",
    )
    ap.add_argument(
        "--no-checkpoint", dest="checkpoint", action="store_false", help="write straight through"
    )
    ap.add_argument("--list", action="store_true", help="list recent pieces and queued topics")
    ap.add_argument("--dry-run", action="store_true", help="print the first prompt, run nothing")
    ap.add_argument("--scan", action="store_true", help="the radar's news scan, when it is due")
    ap.add_argument("--scan-now", action="store_true", help="the radar's news scan now")
    ap.add_argument(
        "--learn",
        action="store_true",
        help="measure the posted pieces against X; rewrite the playbook when due",
    )
    ap.add_argument(
        "--learn-now", action="store_true", help="like --learn, rewriting the playbook now"
    )
    ap.add_argument("-v", "--verbose", action="store_true")
    return ap.parse_args(argv)


def safe_console() -> None:
    """Make print and the log handler show what the console cannot, never raise on it. On
    Windows a piped stdout (the runs page, run_ops.py, Task Scheduler's log file) is the
    ANSI code page, cp1252, with errors='strict', and the session's narration, its queries
    and a piece's title are full of arrows, >= signs and Greek letters (the beta of
    TGF-beta): one of those ended the line's print, and --list or --dry-run with it. Such a
    character is written as its escape (\\u2265) instead."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="backslashreplace")
        except (AttributeError, ValueError):  # not a text stream of its own, or closed
            pass


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    args = _parse_args(argv)
    safe_console()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    from studio import runner

    if args.list:
        return runner.print_list()
    if args.scan or args.scan_now:
        return runner.scan(dry_run=args.dry_run, force=args.scan_now)
    if args.learn or args.learn_now:
        return runner.learn(dry_run=args.dry_run, force=args.learn_now)
    return runner.run(
        now=args.now or bool(args.topic) or args.story is not None,
        resume_only=args.resume_only,
        topic=args.topic,
        story=args.story,
        angle=args.angle,
        checkpoint=args.checkpoint,
        dry_run=args.dry_run,
        trial=args.trial,
    )


if __name__ == "__main__":
    sys.exit(main())
