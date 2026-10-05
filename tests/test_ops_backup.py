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


# ---- files kept with the database (backups.with_db) -------------------------------------


def test_the_studio_playbook_is_kept_with_each_backup_and_rotated_with_it(src, tmp_path):
    """The editor's playbook lives only beside the database (studio_playbook.md): each
    backup carries a copy of it, and loses it when the backup itself is rotated out."""
    playbook = src.parent / "studio_playbook.md"
    dest_dir = tmp_path / "backups"
    for i in range(3):
        playbook.write_text(f"# Playbook, version {i}\n", encoding="utf-8")
        out = backup.backup(
            src, dest_dir, keep=2, now=NOW + timedelta(hours=i), with_db=["studio_playbook.md"]
        )
    assert out.name == "pipeline-20260601T140000Z.sqlite"
    kept = backup.companion_name(out, "studio_playbook.md")
    assert kept.name == "pipeline-20260601T140000Z.studio_playbook.md"
    assert kept.read_text(encoding="utf-8") == "# Playbook, version 2\n"
    assert sorted(p.name for p in dest_dir.iterdir()) == [
        "pipeline-20260601T130000Z.sqlite",
        "pipeline-20260601T130000Z.studio_playbook.md",
        "pipeline-20260601T140000Z.sqlite",
        "pipeline-20260601T140000Z.studio_playbook.md",
    ]
    assert backup.latest_backup(dest_dir) == (out, NOW + timedelta(hours=2))


def test_a_file_not_saved_yet_is_skipped_and_a_same_second_backup_keeps_its_own(src, tmp_path):
    dest_dir = tmp_path / "backups"
    first = backup.backup(src, dest_dir, keep=5, now=NOW, with_db=["studio_playbook.md"])
    assert sorted(p.name for p in dest_dir.iterdir()) == [first.name]  # no playbook yet
    (src.parent / "studio_playbook.md").write_text("mine\n", encoding="utf-8")
    second = backup.backup(src, dest_dir, keep=5, now=NOW, with_db=["studio_playbook.md"])
    assert second.name == "pipeline-20260601T120000Z-1.sqlite"
    assert (dest_dir / "pipeline-20260601T120000Z-1.studio_playbook.md").read_text() == "mine\n"
    # Rotation takes each backup's own files with it, never another's: the two names
    # differ only after the stamp (list_backups orders the -1 copy first).
    [gone] = backup.rotate(dest_dir, 1)
    assert gone.name == second.name
    assert sorted(p.name for p in dest_dir.iterdir()) == [first.name]


def test_the_shipped_settings_keep_the_studio_playbook_and_both_backup_paths_use_them(
    src, tmp_path, monkeypatch
):
    """`run_ops.py backup` and the panel's "Back up now" (and the automatic runs' daily
    backup) read backups.with_db from ops/config.yaml."""
    import run_ops
    from ops.config import load_ops_config
    from panel import app as panel_app

    assert load_ops_config()["backups"]["with_db"] == ["studio_playbook.md"]
    (src.parent / "studio_playbook.md").write_text("mine\n", encoding="utf-8")
    cfg = tmp_path / "ops.yaml"
    cfg.write_text(
        f"backups:\n  dir: {tmp_path / 'cli'}\n  keep: 3\n  with_db: [studio_playbook.md]\n"
    )
    assert run_ops.main(["--config", str(cfg), "--db", str(src), "backup"]) == 0
    assert [p.name.split(".", 1)[1] for p in sorted((tmp_path / "cli").iterdir())] == [
        "sqlite",
        "studio_playbook.md",
    ]
    monkeypatch.setenv("DB_PATH", str(src))
    monkeypatch.setitem(
        panel_app.CONFIG,
        "backups",
        {"dir": str(tmp_path / "panel"), "keep": 3, "with_db": ["studio_playbook.md"]},
    )
    out = panel_app._backup_now(NOW)
    assert backup.companion_name(out, "studio_playbook.md").read_text() == "mine\n"
