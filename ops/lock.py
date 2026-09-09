"""Single-instance lock for the orchestrator (safe under overlapping cron fires).

The lock file stores the holder's pid and started_at as JSON. Acquisition is
non-blocking: a second acquire returns None. A lock file whose pid is no longer
alive is reclaimed with a WARNING; one whose pid is alive is respected even if
the flock itself is free (a holder on a filesystem without flock support).
"""

from __future__ import annotations

import fcntl
import json
import logging
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import IO

log = logging.getLogger(__name__)


@dataclass
class Lock:
    path: Path
    pid: int
    started_at: datetime
    _fh: IO[str] | None

    def release(self) -> None:
        if self._fh is None:
            return
        try:
            self._fh.seek(0)
            self._fh.truncate()
            self._fh.flush()
            fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
        finally:
            self._fh.close()
            self._fh = None

    def __enter__(self) -> Lock:
        return self

    def __exit__(self, *exc: object) -> None:
        self.release()


def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def read_holder(path: Path) -> dict | None:
    """Parse the lock file's JSON; None if empty, missing or unparsable."""
    try:
        text = path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if not text:
        return None
    try:
        data = json.loads(text)
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def acquire(path: str | os.PathLike[str]) -> Lock | None:
    """Take the lock or return None immediately if another live process holds it."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fh = open(path, "a+", encoding="utf-8")  # noqa: SIM115 - closed by Lock.release
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        holder = read_holder(path) or {}
        log.info(
            "lock %s held by pid %s since %s", path, holder.get("pid"), holder.get("started_at")
        )
        fh.close()
        return None

    holder = read_holder(path)
    if holder:
        pid = int(holder.get("pid") or 0)
        if pid != os.getpid() and pid_alive(pid):
            log.info("lock %s belongs to live pid %s; not taking it", path, pid)
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
            fh.close()
            return None
        log.warning(
            "reclaiming stale lock %s from dead pid %s (started %s)",
            path,
            pid,
            holder.get("started_at"),
        )

    started = datetime.now(UTC).replace(microsecond=0)
    fh.seek(0)
    fh.truncate()
    fh.write(json.dumps({"pid": os.getpid(), "started_at": started.isoformat()}))
    fh.flush()
    os.fsync(fh.fileno())
    return Lock(path=path, pid=os.getpid(), started_at=started, _fh=fh)
