"""ops/backup.py against a temp SQLite file."""

import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

from ops import backup

NOW = datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC)


@pytest.fixture
def src(tmp_path):
    path = tmp_path / "pipeline.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)")
    conn.executemany("INSERT INTO t (v) VALUES (?)", [("a",), ("b",), ("c",)])
    conn.commit()
    conn.close()
    return path


def test_backup_copy_opens_and_passes_integrity(src, tmp_path):
    dest_dir = tmp_path / "backups"
    out = backup.backup(src, dest_dir, keep=5, now=NOW)
    assert out.name == "pipeline-20260601T120000Z.sqlite"
    assert out.parent == dest_dir
    conn = sqlite3.connect(out)
    assert conn.execute("SELECT COUNT(*) FROM t").fetchone()[0] == 3
    conn.close()
    assert backup.integrity_check(out) == "ok"
    latest = backup.latest_backup(dest_dir)
    assert latest == (out, NOW)


def test_rotation_keeps_exactly_keep_files(src, tmp_path):
    dest_dir = tmp_path / "backups"
    for i in range(5):
        backup.backup(src, dest_dir, keep=2, now=NOW + timedelta(hours=i))
    names = sorted(p.name for p in dest_dir.iterdir())
    assert names == ["pipeline-20260601T150000Z.sqlite", "pipeline-20260601T160000Z.sqlite"]
    assert backup.latest_backup(dest_dir)[1] == NOW + timedelta(hours=4)


def test_same_second_backups_get_distinct_names(src, tmp_path):
    a = backup.backup(src, tmp_path / "b", keep=5, now=NOW)
    b = backup.backup(src, tmp_path / "b", keep=5, now=NOW)
    assert a != b and b.name == "pipeline-20260601T120000Z-1.sqlite"
    assert len(backup.list_backups(tmp_path / "b")) == 2


def test_corrupt_copy_is_deleted_and_raises(src, tmp_path, monkeypatch):
    monkeypatch.setattr(backup, "integrity_check", lambda p: "*** in database main *** bad")
    dest_dir = tmp_path / "backups"
    with pytest.raises(RuntimeError, match="integrity_check"):
        backup.backup(src, dest_dir, keep=5, now=NOW)
    assert list(dest_dir.iterdir()) == []


def test_latest_backup_empty_or_missing_dir(tmp_path):
    assert backup.latest_backup(tmp_path) is None
    assert backup.latest_backup(tmp_path / "nope") is None
    (tmp_path / "notes.txt").write_text("x")
    (tmp_path / "pipeline-garbage.sqlite").write_text("x")
    assert backup.latest_backup(tmp_path) is None


def test_missing_source_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        backup.backup(tmp_path / "missing.db", tmp_path / "b", keep=1)
