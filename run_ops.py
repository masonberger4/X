"""Operations CLI (step 5): orchestrate the pipeline under cron, check health, alert, back up.

  python run_ops.py run [--only a,b] [--dry-run]   # lock, run steps in order, record, health+alert
  python run_ops.py health [--json] [--alert]      # checks; exit 1 if overall is 'fail'
  python run_ops.py backup [--keep N]
  python run_ops.py status
  python run_ops.py prune --days N                 # ops-owned tables only

Exit codes for `run`: 0 ok, 1 a required step failed, 2 the lock was held.
Nothing here posts to X, calls the Anthropic API, or edits pipeline content.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from ops import alert, backup, health, lock, runner, store
from ops.config import load_ops_config

log = logging.getLogger("run_ops")

REPO_ROOT = Path(__file__).resolve().parent


def _now() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def _age(now: datetime, then: datetime | None) -> str:
    if then is None:
        return "never"
    return health._fmt_hours((now - then).total_seconds() / 3600.0) + " ago"


def _disk_free_mb(db_path: Path) -> float | None:
    try:
        where = db_path.parent if db_path.parent.exists() else Path(".")
        return shutil.disk_usage(where).free / (1024 * 1024)
    except OSError:
        return None


def _db_path(args: argparse.Namespace) -> Path:
    return Path(args.db) if args.db else store.db_path()


# ---------------------------------------------------------------------------
# health (shared by `run`, `health`, `status`)
# ---------------------------------------------------------------------------


def build_report(conn, cfg: dict[str, Any], db_path: Path, now: datetime) -> health.Report:
    th = health.Thresholds.from_config(cfg.get("health"))
    db_size_mb = db_path.stat().st_size / (1024 * 1024) if db_path.exists() else None
    disk_free_mb: float | None
    try:
        disk_free_mb = shutil.disk_usage(
            db_path.parent if db_path.parent.exists() else "."
        ).free / (1024 * 1024)
    except OSError:
        disk_free_mb = None
    env_present = {name: bool(os.environ.get(name)) for name in th.required_env}
    backend = store.configured_backend()
    return health.run_all(
        backend=backend,
        now=now,
        thresholds=th,
        source_runs=store.fetch_source_runs(conn),
        activity=store.fetch_stage_activity(conn, now),
        publish=store.fetch_publish_state(conn, now),
        feedback=store.fetch_feedback_state(conn),
        latest_backup=backup.latest_backup(cfg["backups"]["dir"]),
        db_size_mb=db_size_mb,
        disk_free_mb=disk_free_mb,
        env_present=env_present,
        table_counts=store.table_counts(conn),
    )


def health_and_alert(conn, cfg: dict[str, Any], db_path: Path, now: datetime, send: bool):
    report = build_report(conn, cfg, db_path, now)
    store.record_health(conn, report)
    if send:
        prior = store.last_alert_per_check(conn)
        for s in alert.notify(report, prior, cfg, now=now):
            store.record_alert(conn, s.check_name, s.status, s.channel, s.sent_at)
    else:
        alert.log_report(report)
    return report


# ---------------------------------------------------------------------------
# subcommands
# ---------------------------------------------------------------------------


def cmd_run(args: argparse.Namespace, cfg: dict[str, Any]) -> int:
    steps = runner.steps_from_config(cfg)
    only = [s.strip() for s in args.only.split(",") if s.strip()] if args.only else None
    if only:
        unknown = set(only) - {s.name for s in steps}
        if unknown:
            log.error("unknown step(s) in --only: %s", ", ".join(sorted(unknown)))
            return 1
    tail = int(cfg.get("run_log_tail_chars", 4000))

    if args.dry_run:
        results = runner.run_steps(steps, only=only, dry_run=True, cwd=REPO_ROOT, tail_chars=tail)
        for r in results:
            print(f"{r.name:<10} {r.skipped_reason:<16} {' '.join(r.argv)}")
        return 0

    held = lock.acquire(cfg["lock_path"])
    if held is None:
        log.error("another run holds the lock %s; exiting", cfg["lock_path"])
        return 2
    with held:
        run_id = f"{_now().strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:6]}"
        log.info("run %s starting (%s)", run_id, ", ".join(only or [s.name for s in steps]))
        results = runner.run_steps(steps, only=only, cwd=REPO_ROOT, tail_chars=tail)
        db_path = _db_path(args)
        conn = store.connect(db_path)
        try:
            store.record_results(conn, run_id, results)
            health_and_alert(conn, cfg, db_path, _now(), send=True)
        finally:
            conn.close()
    required = {s.name for s in steps if s.required}
    failed = [r.name for r in results if r.failed and r.name in required]
    if failed:
        log.error("run %s: required step(s) failed: %s", run_id, ", ".join(failed))
        return 1
    log.info("run %s finished", run_id)
    return 0


def cmd_health(args: argparse.Namespace, cfg: dict[str, Any]) -> int:
    db_path = _db_path(args)
    conn = store.connect(db_path)
    try:
        report = health_and_alert(conn, cfg, db_path, _now(), send=args.alert)
    finally:
        conn.close()
    if args.json:
        print(json.dumps(report.to_dict(), indent=2))
    else:
        print(health.format_report(report))
    return 1 if report.overall == "fail" else 0


def cmd_backup(args: argparse.Namespace, cfg: dict[str, Any]) -> int:
    keep = args.keep if args.keep is not None else int(cfg["backups"]["keep"])
    try:
        out = backup.backup(_db_path(args), cfg["backups"]["dir"], keep)
    except (FileNotFoundError, RuntimeError) as exc:
        log.error("backup failed: %s", exc)
        return 1
    print(out)
    return 0


def cmd_status(args: argparse.Namespace, cfg: dict[str, Any]) -> int:
    now = _now()
    db_path = _db_path(args)
    conn = store.connect(db_path)
    try:
        last_runs = store.last_run_per_step(conn)
        last_health = store.last_health(conn)
        counts = store.table_counts(conn)
    finally:
        conn.close()
    lines = [f"Pipeline status at {now.isoformat()}  (db: {db_path})", "", "Last run per step:"]
    for step in runner.steps_from_config(cfg):
        r = last_runs.get(step.name)
        if r is None:
            lines.append(f"  {step.name:<10} never")
            continue
        if r["skipped_reason"]:
            outcome = f"skipped ({r['skipped_reason']})"
        elif r["timed_out"]:
            outcome = "TIMED OUT"
        else:
            outcome = f"exit {r['exit_code']}"
        lines.append(f"  {step.name:<10} {outcome:<22} {_age(now, r['finished_at'])}")
    lines.append("")
    if last_health is None:
        lines.append("Last health: never")
    else:
        lines.append(
            f"Last health: {last_health['overall'].upper()} "
            f"({_age(now, last_health['checked_at'])})"
        )
        for c in last_health["report"]["checks"]:
            if c["status"] != "ok":
                lines.append(f"  [{c['status']:<4}] {c['name']:<10} {c['summary']}")
    lines.append("")
    lines.append("Table row counts:")
    for name, n in counts.items():
        lines.append(f"  {name:<20} {n}")
    lines.append("")
    latest = backup.latest_backup(cfg["backups"]["dir"])
    if latest is None:
        lines.append(f"Latest backup: none in {cfg['backups']['dir']}")
    else:
        lines.append(f"Latest backup: {latest[0].name} ({_age(now, latest[1])})")
    size = db_path.stat().st_size / (1024 * 1024) if db_path.exists() else 0.0
    free = _disk_free_mb(db_path)
    free_s = "?" if free is None else f"{free:.0f} MB"
    lines.append(f"DB size: {size:.1f} MB   Disk free: {free_s}")
    print("\n".join(lines))
    return 0


def cmd_prune(args: argparse.Namespace, cfg: dict[str, Any]) -> int:
    conn = store.connect(_db_path(args))
    try:
        deleted = store.prune_own(conn, args.days)
    finally:
        conn.close()
    for table, n in deleted.items():
        print(f"{table:<16} deleted {n}")
    return 0


# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--config", default=None, help="path to ops/config.yaml override")
    ap.add_argument("--db", default=None, help="SQLite path (default: DB_PATH / config db_path)")
    ap.add_argument("-v", "--verbose", action="store_true")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("run", help="run the configured steps in order, then health + alerts")
    p.add_argument("--only", default=None, help="comma-separated subset of step names")
    p.add_argument("--dry-run", action="store_true", help="print argv, run nothing, record nothing")
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("health", help="run health checks and print a report")
    p.add_argument("--json", action="store_true")
    p.add_argument("--alert", action="store_true", help="also send notifications")
    p.set_defaults(func=cmd_health)

    p = sub.add_parser("backup", help="online backup of the SQLite file, verified and rotated")
    p.add_argument("--keep", type=int, default=None)
    p.set_defaults(func=cmd_backup)

    p = sub.add_parser("status", help="one-screen summary")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("prune", help="delete old rows from ops-owned tables only")
    p.add_argument("--days", type=int, required=True)
    p.set_defaults(func=cmd_prune)
    return ap


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-5s %(name)s: %(message)s",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    cfg = load_ops_config(args.config)
    return int(args.func(args, cfg))


if __name__ == "__main__":
    sys.exit(main())
