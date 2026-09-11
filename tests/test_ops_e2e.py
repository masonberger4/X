"""End-to-end run_ops tests against a temp SQLite file.

Step 1 tables come from db.Database, step 2 from approval_queue.store.connect (conftest's
db_file/conn fixtures), step 3's schedule/posts are created by hand here. Nothing runs a
real pipeline stage: `run` tests use python -c steps or --dry-run.
"""

import json
import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta

import pytest
import yaml

import run_ops
from approval_queue import store as qstore
from db import Database
from draft.schema import Draft
from ops import lock, store
from ops.config import DEFAULT_OPS_CONFIG_PATH, load_ops_config
from tests.conftest import seed_item

SOURCES = [
    {"name": "pubmed", "cadence_minutes": 60, "enabled": True},
    {"name": "fda_press", "cadence_minutes": 60, "enabled": True},
    {"name": "nejm", "cadence_minutes": 60, "enabled": True},
]

PUBLISH_SCHEMA = """
CREATE TABLE IF NOT EXISTS schedule (
    id INTEGER PRIMARY KEY AUTOINCREMENT, draft_id INTEGER NOT NULL UNIQUE,
    scheduled_for TEXT, claimed_at TEXT, finished_at TEXT,
    status TEXT NOT NULL DEFAULT 'pending', error TEXT);
CREATE TABLE IF NOT EXISTS posts (
    id INTEGER PRIMARY KEY AUTOINCREMENT, draft_id INTEGER NOT NULL, tweet_id TEXT,
    text TEXT NOT NULL, kind TEXT NOT NULL, position INTEGER NOT NULL DEFAULT 1,
    posted_at TEXT, slot TEXT, status TEXT NOT NULL, error TEXT);
"""


@pytest.fixture
def ops_cfg(tmp_path):
    """A copy of the default ops/config.yaml with tmp lock/backup paths and no real steps."""
    cfg = yaml.safe_load(DEFAULT_OPS_CONFIG_PATH.read_text())
    cfg["lock_path"] = str(tmp_path / "pipeline.lock")
    cfg["backups"]["dir"] = str(tmp_path / "backups")
    cfg["alerts"]["channels"]["webhook"] = False
    path = tmp_path / "ops.yaml"
    path.write_text(yaml.safe_dump(cfg))
    return path


@pytest.fixture
def seeded(conn, db_file, monkeypatch):
    now = datetime.now(UTC)
    d = Database(str(db_file))
    d.record_run("pubmed", 12, 3, at=now - timedelta(minutes=20))
    d.record_run("fda_press", 0, 0, error="HTTP 503 from feed", at=now - timedelta(minutes=20))
    # nejm: enabled in config but never ran
    d.close()
    cid = seed_item(conn, "a", hours_ago=1)
    seed_item(conn, "b", hours_ago=2)
    draft = Draft(
        single_post="p https://doi.org/10.1000/x",
        thread=["one", "two", "three"],
        suggested_visual="",
        why_it_matters="",
    )
    did = qstore.insert_draft(conn, item_id="a", cluster_id=cid, model="m", draft=draft)
    qstore.approve(conn, did)
    conn.executescript(PUBLISH_SCHEMA)
    when = (now - timedelta(hours=3)).isoformat()
    conn.execute(
        "INSERT INTO schedule (draft_id, status, claimed_at, finished_at, error)"
        " VALUES (?, 'partial', ?, ?, 'post 2 failed')",
        (did, when, when),
    )
    conn.execute(
        "INSERT INTO posts (draft_id, tweet_id, text, kind, position, posted_at, status)"
        " VALUES (?, '1', 'one', 'thread', 1, ?, 'posted')",
        (did, when),
    )
    conn.commit()
    monkeypatch.setattr(store, "configured_sources", lambda: SOURCES)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-not-real")
    return db_file


def test_health_json_reports_failing_source_partial_thread_and_feedback_skip(
    seeded, ops_cfg, capsys
):
    rc = run_ops.main(["--config", str(ops_cfg), "health", "--json"])
    out = capsys.readouterr().out
    report = json.loads(out)
    checks = {c["name"]: c for c in report["checks"]}
    assert rc == 1 and report["overall"] == "fail"

    sources = checks["sources"]
    assert sources["status"] == "fail"  # 2 of 3 (error + never ran) >= 0.34
    assert sources["details"]["error"] == ["fda_press"]
    assert sources["details"]["never_ran"] == ["nejm"]
    assert "HTTP 503" not in json.dumps(sources)  # error text stays in the DB

    assert checks["publish"]["status"] == "fail"
    assert checks["publish"]["details"]["partial_7d"] == 1
    assert "partial" in checks["publish"]["summary"]
    assert checks["feedback"]["status"] == "skip"
    assert checks["env"]["status"] == "ok"
    assert checks["staleness"]["status"] == "ok"
    assert checks["backlog"]["details"]["approved_drafts"] == 1
    assert checks["storage"]["details"]["table_counts"]["items"] == 2
    assert "test-key-not-real" not in out

    # The report was stored in health_checks.
    conn = store.connect(seeded)
    assert store.last_health(conn)["overall"] == "fail"
    conn.close()


def test_health_text_report(seeded, ops_cfg, capsys):
    run_ops.main(["--config", str(ops_cfg), "health"])
    out = capsys.readouterr().out
    assert out.startswith("Pipeline health at ")
    assert "[fail] publish" in out and "[skip] feedback" in out


def test_run_dry_run_records_nothing_and_runs_nothing(seeded, ops_cfg, capsys, monkeypatch):
    def explode(*a, **k):
        raise AssertionError("no subprocess in --dry-run")

    monkeypatch.setattr(subprocess, "run", explode)
    rc = run_ops.main(["--config", str(ops_cfg), "run", "--only", "ingest", "--dry-run"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "run_ingest.py" in out and "dry run" in out
    conn = store.connect(seeded)
    assert conn.execute("SELECT COUNT(*) FROM pipeline_runs").fetchone()[0] == 0
    conn.close()
    assert not os.path.exists(yaml.safe_load(ops_cfg.read_text())["lock_path"])


def test_status_prints(seeded, ops_cfg, capsys):
    rc = run_ops.main(["--config", str(ops_cfg), "status"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "Last run per step" in out and "ingest     never" in out
    assert "Table row counts" in out and "items" in out
    assert "Latest backup: none" in out


def test_run_records_results_and_exit_codes(seeded, ops_cfg, tmp_path, capsys):
    cfg = yaml.safe_load(ops_cfg.read_text())
    cfg["steps"] = [
        {"name": "good", "argv": ["python", "-c", "print('ok')"], "required": True},
        {"name": "bad", "argv": ["python", "-c", "import sys; sys.exit(4)"], "required": True},
        {"name": "after", "argv": ["python", "-c", "print('x')"], "required": False},
        {"name": "off", "argv": ["python", "-c", "print('x')"], "enabled": False},
    ]
    ops_cfg.write_text(yaml.safe_dump(cfg))
    rc = run_ops.main(["--config", str(ops_cfg), "run"])
    assert rc == 1
    conn = store.connect(seeded)
    last = store.last_run_per_step(conn)
    assert last["good"]["exit_code"] == 0 and last["good"]["stdout_tail"].strip() == "ok"
    assert last["bad"]["exit_code"] == 4
    assert last["after"]["skipped_reason"] == "upstream failed"
    assert last["off"]["skipped_reason"] == "disabled"
    assert store.last_health(conn) is not None  # health ran after the steps
    conn.close()

    # A successful required-only run exits 0 and `status` shows it.
    cfg["steps"] = cfg["steps"][:1]
    ops_cfg.write_text(yaml.safe_dump(cfg))
    assert run_ops.main(["--config", str(ops_cfg), "run"]) == 0
    run_ops.main(["--config", str(ops_cfg), "status"])
    assert "good       exit 0" in capsys.readouterr().out


def test_run_exits_2_when_lock_held(seeded, ops_cfg):
    cfg = yaml.safe_load(ops_cfg.read_text())
    with lock.acquire(cfg["lock_path"]):
        assert run_ops.main(["--config", str(ops_cfg), "run", "--only", "ingest"]) == 2
    conn = store.connect(seeded)
    assert conn.execute("SELECT COUNT(*) FROM pipeline_runs").fetchone()[0] == 0
    conn.close()


def test_run_unknown_only_step(seeded, ops_cfg):
    assert run_ops.main(["--config", str(ops_cfg), "run", "--only", "nope"]) == 1


def test_backup_and_prune_subcommands(seeded, ops_cfg, capsys):
    rc = run_ops.main(["--config", str(ops_cfg), "backup", "--keep", "1"])
    out = capsys.readouterr().out.strip()
    assert rc == 0 and out.endswith(".sqlite") and os.path.exists(out)
    run_ops.main(["--config", str(ops_cfg), "status"])
    assert "Latest backup: pipeline-" in capsys.readouterr().out
    rc = run_ops.main(["--config", str(ops_cfg), "prune", "--days", "30"])
    assert rc == 0 and "pipeline_runs" in capsys.readouterr().out


def test_health_alert_flag_records_alerts(seeded, ops_cfg, monkeypatch):
    calls = []
    monkeypatch.setattr(
        run_ops.alert, "post_webhook", lambda url, payload, timeout=10: calls.append(payload)
    )
    monkeypatch.setenv("ALERT_WEBHOOK_URL", "https://hooks.example/x")
    cfg = yaml.safe_load(ops_cfg.read_text())
    cfg["alerts"]["channels"]["webhook"] = True
    ops_cfg.write_text(yaml.safe_dump(cfg))
    run_ops.main(["--config", str(ops_cfg), "health", "--alert"])
    assert len(calls) == 1 and "FAIL publish" in calls[0]["text"]
    conn = store.connect(seeded)
    prior = store.last_alert_per_check(conn)
    assert prior["publish"][0] == "fail" and prior["sources"][0] == "fail"
    conn.close()
    # Second call inside the cooldown sends nothing.
    run_ops.main(["--config", str(ops_cfg), "health", "--alert"])
    assert len(calls) == 1


def test_health_on_bare_db_skips_everything_but_env_and_storage(
    tmp_path, ops_cfg, monkeypatch, capsys
):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "bare.db"))
    monkeypatch.setattr(store, "configured_sources", lambda: [])
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    run_ops.main(["--config", str(ops_cfg), "health", "--json"])
    report = json.loads(capsys.readouterr().out)
    statuses = {c["name"]: c["status"] for c in report["checks"]}
    assert statuses["sources"] == "skip" and statuses["staleness"] == "skip"
    assert statuses["publish"] == "skip" and statuses["feedback"] == "skip"
    assert statuses["backups"] == "warn"  # no backup yet


# ---- safety of the shipped config ------------------------------------------------


def test_default_ops_config_never_contains_live():
    text = DEFAULT_OPS_CONFIG_PATH.read_text()
    assert "--live" not in text
    cfg = load_ops_config()
    steps = {s["name"]: s for s in cfg["steps"]}
    assert [s["name"] for s in cfg["steps"]] == [
        "ingest",
        "score",
        "draft",
        "verify",
        "publish",
        "feedback",
    ]
    assert steps["verify"]["enabled"] and not steps["verify"]["required"]
    # the shipped publish step runs, but as a dry run: its argv never carries --live
    assert steps["publish"]["enabled"] is True
    assert steps["publish"]["argv"] == ["python", "run_publish.py"]
    assert steps["feedback"]["enabled"] is False and steps["feedback"]["required"] is False
    for name in ("ingest", "score", "draft"):
        assert steps[name]["enabled"] and steps[name]["required"]
    for step in cfg["steps"]:
        assert "--live" not in step["argv"]


def test_default_config_dry_run_lists_real_clis(capsys):
    """With the default config, --dry-run resolves every enabled step to an existing CLI."""
    rc = run_ops.main(["run", "--dry-run"])
    out = capsys.readouterr().out
    assert rc == 0
    for line in out.splitlines():
        name, reason = line.split()[:2]
        if name in ("ingest", "score", "draft"):
            assert reason == "dry" and sys.executable in line
        if name == "feedback":
            assert reason in ("disabled", "not")  # not merged on this checkout, or disabled
