"""SQLite backups via the online backup API, verified with PRAGMA integrity_check.

Restore is a manual step: stop the cron/timer, copy the chosen
backups/pipeline-<stamp>.sqlite over the DB path, restart. See deploy/README.md.
"""

from __future__ import annotations

import logging
import re
import sqlite3
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
    db_path: str | Path, dest_dir: str | Path, keep: int, now: datetime | None = None
) -> Path:
    """Copy the live DB to dest_dir/pipeline-<UTC stamp>.sqlite, verify it, rotate."""
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

    src = sqlite3.connect(str(src_path))
    try:
        dst = sqlite3.connect(str(dest))
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()

    result = integrity_check(dest)
    if result != "ok":
        dest.unlink(missing_ok=True)
        raise RuntimeError(f"backup {dest.name} failed integrity_check: {result}")
    size_mb = dest.stat().st_size / (1024 * 1024)
    log.info("backup written: %s (%.1f MB, integrity ok)", dest, size_mb)
    removed = rotate(dest_dir, keep)
    if removed:
        log.info("rotated %d old backup(s): %s", len(removed), ", ".join(p.name for p in removed))
    return dest


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
    return removed


def latest_backup(dest_dir: str | Path) -> tuple[Path, datetime] | None:
    backups = list_backups(dest_dir)
    return backups[-1] if backups else None
