"""SQLite backups via the online backup API, verified with PRAGMA integrity_check.

Each backup can carry files that live beside the database and nowhere else (`with_db`, from
`backups.with_db` in ops/config.yaml: the studio's playbook, which the editor writes on the
studio page), copied as backups/pipeline-<stamp>.<name> and rotated with it.

Restore is a manual step: stop the cron/timer, copy the chosen
backups/pipeline-<stamp>.sqlite over the DB path (and pipeline-<stamp>.<name> back to <name>
beside it), restart. See deploy/README.md.
"""

from __future__ import annotations

import glob
import logging
import re
import shutil
import sqlite3
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path

log = logging.getLogger(__name__)

PREFIX = "pipeline-"
SUFFIX = ".sqlite"
STAMP = "%Y%m%dT%H%M%SZ"
_NAME_RE = re.compile(r"^pipeline-(\d{8}T\d{6}Z)(?:-\d+)?\.sqlite$")


def backup_name(now: datetime) -> str:
    return f"{PREFIX}{now.astimezone(UTC).strftime(STAMP)}{SUFFIX}"


def integrity_check(path: Path) -> str:
    """First line of PRAGMA integrity_check ('ok' when the file is sound)."""
    conn = sqlite3.connect(str(path))
    try:
        row = conn.execute("PRAGMA integrity_check").fetchone()
    finally:
        conn.close()
    return str(row[0]) if row else "no result"


def backup(
    db_path: str | Path,
    dest_dir: str | Path,
    keep: int,
    now: datetime | None = None,
    with_db: Iterable[str] = (),
) -> Path:
    """Copy the live DB to dest_dir/pipeline-<UTC stamp>.sqlite, verify it, copy each file
    named in `with_db` from the DB's folder beside it (`companion_name`), rotate."""
    src_path = Path(db_path)
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    if not src_path.exists():
        raise FileNotFoundError(f"database not found: {src_path}")
    now = now or datetime.now(UTC)
    dest = dest_dir / backup_name(now)
    n = 1
    while dest.exists():  # two backups inside one second
        dest = dest_dir / f"{PREFIX}{now.astimezone(UTC).strftime(STAMP)}-{n}{SUFFIX}"
        n += 1

    # Written under a .part name and renamed only once it checks out, so a backup cut short
    # (the app closed mid-copy) never looks like the newest good backup to latest_backup().
    part = dest.with_name(dest.name + ".part")
    src = sqlite3.connect(str(src_path))
    try:
        dst = sqlite3.connect(str(part))
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()

    result = integrity_check(part)
    if result != "ok":
        part.unlink(missing_ok=True)
        raise RuntimeError(f"backup {dest.name} failed integrity_check: {result}")
    part.replace(dest)
    size_mb = dest.stat().st_size / (1024 * 1024)
    log.info("backup written: %s (%.1f MB, integrity ok)", dest, size_mb)
    for name in with_db:
        _copy_beside(src_path.parent / Path(name).name, dest)
    removed = rotate(dest_dir, keep)
    if removed:
        log.info("rotated %d old backup(s): %s", len(removed), ", ".join(p.name for p in removed))
    return dest


def companion_name(backup_path: Path, name: str) -> Path:
    """Where a file kept with a backup goes: pipeline-<stamp>.<name>, beside it."""
    return backup_path.with_name(f"{backup_path.stem}.{Path(name).name}")


def _copy_beside(source: Path, backup_path: Path) -> None:
    """Copy one file kept with the database beside its backup. A file that is not there
    (no playbook saved yet) is skipped; one that cannot be copied costs a warning, never the
    database's backup, which is already written."""
    if not source.is_file():
        return
    target = companion_name(backup_path, source.name)
    part = target.with_name(target.name + ".part")
    try:
        shutil.copy2(source, part)
        part.replace(target)
    except OSError as exc:
        part.unlink(missing_ok=True)
        log.warning("could not keep %s with the backup: %s", source.name, exc)
        return
    log.info("kept with the backup: %s", target.name)


def list_backups(dest_dir: str | Path) -> list[tuple[Path, datetime]]:
    """All backups in dest_dir, oldest first, with the timestamp parsed from the name."""
    dest_dir = Path(dest_dir)
    if not dest_dir.is_dir():
        return []
    out: list[tuple[Path, datetime]] = []
    for p in dest_dir.iterdir():
        m = _NAME_RE.match(p.name)
        if not m or not p.is_file():
            continue
        when = datetime.strptime(m.group(1), STAMP).replace(tzinfo=UTC)
        out.append((p, when))
    out.sort(key=lambda t: (t[1], t[0].name))
    return out


def rotate(dest_dir: str | Path, keep: int) -> list[Path]:
    """Delete all but the `keep` newest backups; returns what was removed."""
    keep = max(0, int(keep))
    backups = list_backups(dest_dir)
    removed: list[Path] = []
    excess = len(backups) - keep
    for p, _ in backups[:excess] if excess > 0 else []:
        p.unlink(missing_ok=True)
        removed.append(p)
        # the files kept with it (pipeline-<stamp>.<name>) go with it
        for extra in Path(dest_dir).glob(glob.escape(p.stem) + ".*"):
            extra.unlink(missing_ok=True)
    return removed


def latest_backup(dest_dir: str | Path) -> tuple[Path, datetime] | None:
    backups = list_backups(dest_dir)
    return backups[-1] if backups else None
