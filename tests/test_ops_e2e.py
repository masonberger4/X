"""End-to-end run_ops tests against a temp SQLite file.

Step 1 tables come from db.Database, step 2 from approval_queue.store.connect (conftest's
db_file/conn fixtures), step 3's schedule/posts are created by hand here. Nothing runs a
real pipeline stage: `run` tests use python -c steps or --dry-run. Health's lookup of the
Claude Code CLI is pinned (cli_on_path), so no report depends on this machine's PATH.
"""

import json
import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

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

CLI_DIR = "/opt/claude-code/bin"


@pytest.fixture
def cli_on_path(monkeypatch):
    """Every model call runs through the Claude Code CLI, and health's "cli" check looks up
    the binary the root config.yaml names (run_ops.cli_status). Pin that lookup to "found"
    so a report never depends on whether the machine running the tests has `claude`."""
    monkeypatch.setattr(run_ops.shutil, "which", lambda name: f"{CLI_DIR}/{name}")


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
def seeded(conn, db_file, monkeypatch, cli_on_path):
    now = datetime.now(UTC)
    d = Database(str(db_file))
    d.record_run("pubmed", 12, 3, at=now - timedelta(minutes=20))
    d.record_run("fda_press", 0, 0, error="HTTP 503 from feed", at=now - timedelta(minutes=20))
    # nejm: enabled in config but never ran
    d.close()
    cid = seed_item(conn, "a", hours_ago=1)
    seed_item(conn, "b", hours_ago=2)
    draft = Draft(
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
    return db_file


def test_health_json_reports_failing_source_partial_thread_and_feedback_skip(
    seeded, ops_cfg, monkeypatch, capsys
):
    # A key left over in an old .env: Claude is reached only through the CLI, so the shipped
    # config neither requires it nor reads it into the report.
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-not-real")
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
    # the CLI is what the report looks for instead: the configured binary, found on PATH
    cli = checks["cli"]
    assert cli["status"] == "ok"
    assert cli["details"]["path"] == f"{CLI_DIR}/{cli['details']['binary']}"
    assert checks["staleness"]["status"] == "ok"
    assert checks["backlog"]["details"]["approved_drafts"] == 1
    assert checks["storage"]["details"]["table_counts"]["items"] == 2
    assert "test-key-not-real" not in out and "ANTHROPIC_API_KEY" not in out

    # The report was stored in health_checks.
    conn = store.connect(seeded)
    assert store.last_health(conn)["overall"] == "fail"
    conn.close()


def test_health_text_report(seeded, ops_cfg, capsys):
    run_ops.main(["--config", str(ops_cfg), "health"])
    out = capsys.readouterr().out
    assert out.startswith("Pipeline health at ")
    assert "[fail] publish" in out and "[skip] feedback" in out
    assert "[ok  ] cli" in out and f"Claude Code CLI at {CLI_DIR}/" in out


def test_health_env_check_names_missing_vars_and_never_prints_a_value(
    seeded, ops_cfg, monkeypatch, capsys
):
    """`health.required_env` holds names only: the report says which are missing, and a set
    var's value is never read into it (build_report passes booleans to the check)."""
    cfg = yaml.safe_load(ops_cfg.read_text())
    cfg["health"]["required_env"] = ["OPS_TEST_SECRET", "OPS_TEST_MISSING"]
    ops_cfg.write_text(yaml.safe_dump(cfg))
    monkeypatch.setenv("OPS_TEST_SECRET", "secret-value-not-real")
    monkeypatch.delenv("OPS_TEST_MISSING", raising=False)
    run_ops.main(["--config", str(ops_cfg), "health", "--json"])
    out = capsys.readouterr().out
    env = {c["name"]: c for c in json.loads(out)["checks"]}["env"]
    assert env["status"] == "fail" and "OPS_TEST_MISSING" in env["summary"]
    assert env["details"]["missing"] == ["OPS_TEST_MISSING"]
    assert env["details"]["required"] == ["OPS_TEST_SECRET", "OPS_TEST_MISSING"]
    assert "secret-value-not-real" not in out


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


def _long_step_config(ops_cfg, tmp_path) -> tuple[dict, Path, Path]:
    """A pipeline step and a skip_when_busy one (the studio's place), each leaving a marker."""
    pipeline, session = tmp_path / "pipeline-ran", tmp_path / "session-ran"
    cfg = yaml.safe_load(ops_cfg.read_text())
    cfg["steps"] = [
        {"name": "ingest", "argv": ["python", "-c", f"open({str(pipeline)!r}, 'w')"]},
        {
            "name": "studio",
            "lock": "studio",
            "skip_when_busy": True,
            "timeout_seconds": 0,
            "argv": ["python", "-c", f"open({str(session)!r}, 'w')"],
        },
    ]
    ops_cfg.write_text(yaml.safe_dump(cfg))
    return cfg, pipeline, session


def test_a_plain_run_leaves_a_long_step_to_its_own_entry(seeded, ops_cfg, tmp_path):
    """Under cron, Task Scheduler or systemd a studio session inside the plain run would
    hold the run and its lock for an hour or more: ingest, score and draft would stop for
    as long. The plain run leaves it out; `--only studio` is its own schedule entry."""
    _, pipeline, session = _long_step_config(ops_cfg, tmp_path)

    assert run_ops.main(["--config", str(ops_cfg), "run"]) == 0
    assert pipeline.exists() and not session.exists()
    conn = store.connect(seeded)
    assert set(store.last_run_per_step(conn)) == {"ingest"}
    conn.close()

    assert run_ops.main(["--config", str(ops_cfg), "run", "--only", "studio"]) == 0
    assert session.exists()


def test_the_long_steps_own_entry_never_takes_the_run_lock(seeded, ops_cfg, tmp_path):
    """The studio's entry runs while a pipeline run holds the run lock, and holds only the
    studio's own lock while it runs, so the pipeline's fires in the meantime go ahead."""
    cfg, pipeline, session = _long_step_config(ops_cfg, tmp_path)
    cfg["steps"][1]["argv"] = [
        "python",
        "-c",
        "import sys; sys.path.insert(0, sys.argv[1]); from ops import lock; "
        f"open({str(session)!r}, 'w').write(str(lock.acquire(sys.argv[2]) is not None))",
        str(run_ops.REPO_ROOT),
        cfg["lock_path"],
    ]
    ops_cfg.write_text(yaml.safe_dump(cfg))

    with lock.acquire(cfg["lock_path"]):  # a pipeline run is going
        assert run_ops.main(["--config", str(ops_cfg), "run", "--only", "studio"]) == 0
    assert session.read_text() == "False"  # still that run's lock, never taken from it

    assert run_ops.main(["--config", str(ops_cfg), "run", "--only", "studio"]) == 0
    assert session.read_text() == "True"  # the step ran without the run lock held
    with lock.acquire(lock.step_lock_path(cfg["lock_path"], "studio")):  # a session is going
        assert run_ops.main(["--config", str(ops_cfg), "run"]) == 0
        assert run_ops.main(["--config", str(ops_cfg), "run", "--only", "studio"]) == 0
    conn = store.connect(seeded)
    assert store.last_run_per_step(conn)["studio"]["skipped_reason"] == "locked"
    conn.close()
    # with a pipeline step named beside it, the run lock is taken as before
    with lock.acquire(cfg["lock_path"]):
        assert run_ops.main(["--config", str(ops_cfg), "run", "--only", "ingest,studio"]) == 2


def test_run_unknown_only_step(seeded, ops_cfg):
    assert run_ops.main(["--config", str(ops_cfg), "run", "--only", "nope"]) == 1


def test_run_refuses_a_step_that_would_post(seeded, ops_cfg, tmp_path):
    """Posting is manual only: a configured step carrying --live stops the whole run before
    anything is spawned, dry run included; --only on the other steps still runs them."""
    marker = tmp_path / "ran"
    cfg = yaml.safe_load(ops_cfg.read_text())
    cfg["steps"] = [
        {"name": "ingest", "argv": ["python", "-c", f"open({str(marker)!r}, 'w')"]},
        {"name": "publish", "argv": ["python", "run_publish.py", "--live"]},
    ]
    ops_cfg.write_text(yaml.safe_dump(cfg))
    assert run_ops.main(["--config", str(ops_cfg), "run"]) == 1
    assert run_ops.main(["--config", str(ops_cfg), "run", "--dry-run"]) == 1
    assert run_ops.main(["--config", str(ops_cfg), "run", "--only", "publish"]) == 1
    assert not marker.exists()
    conn = store.connect(seeded)
    assert conn.execute("SELECT COUNT(*) FROM pipeline_runs").fetchone()[0] == 0
    conn.close()
    assert run_ops.main(["--config", str(ops_cfg), "run", "--only", "ingest"]) == 0
    assert marker.exists()


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
    tmp_path, ops_cfg, cli_on_path, monkeypatch, capsys
):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "bare.db"))
    monkeypatch.setattr(store, "configured_sources", lambda: [])
    for name in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"):  # Claude needs no API key
        monkeypatch.delenv(name, raising=False)
    run_ops.main(["--config", str(ops_cfg), "health", "--json"])
    report = json.loads(capsys.readouterr().out)
    statuses = {c["name"]: c["status"] for c in report["checks"]}
    assert statuses["env"] == "ok" and statuses["cli"] == "ok"
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
        "studio_scan",
        "studio_scan_now",
        "studio",
        "studio_now",
        "studio_resume",
        "draft",
        "draft_retry",
        "verify",
        "publish",
        "feedback",
        "evolve",
        "studio_learn",
        "studio_learn_now",
    ]
    assert steps["verify"]["enabled"] and not steps["verify"]["required"]
    # the retry button's step runs only when named: never in a plain run or automatically
    assert steps["draft_retry"]["manual"] is True and steps["draft_retry"]["lock"] == "draft"
    assert steps["evolve"]["enabled"] is True and steps["evolve"]["required"] is False
    # the shipped publish step runs, but as a dry run: its argv never carries --live
    assert steps["publish"]["enabled"] is True
    assert steps["publish"]["argv"] == ["python", "run_publish.py"]
    assert steps["feedback"]["enabled"] is True and steps["feedback"]["required"] is False
    for name in ("ingest", "score", "draft"):
        assert steps[name]["enabled"] and steps[name]["required"]
    for step in cfg["steps"]:
        assert "--live" not in step["argv"]


def test_the_studio_steps_share_one_lock_and_only_the_plain_one_runs_on_its_own():
    steps = {s["name"]: s for s in load_ops_config()["steps"]}
    studio = ("studio", "studio_now", "studio_resume")
    for name in studio:
        # one session at a time, whichever button or timer started it; a session is often
        # an hour (each stage has its own limit in studio/config.yaml), and it never stops
        # the steps after it
        assert steps[name]["lock"] == "studio" and steps[name]["timeout_seconds"] == 0
        assert steps[name]["enabled"] is True and steps[name]["required"] is False
        assert steps[name]["argv"][:2] == ["python", "run_studio.py"]
    assert steps["studio"]["argv"] == ["python", "run_studio.py"]
    assert not steps["studio"].get("manual")  # in a plain run and the automatic runs
    # a session still going at the next run time sits that run out instead of holding it
    assert steps["studio"]["skip_when_busy"] is True
    # the studio page's buttons: only when named, never on a timer
    assert steps["studio_now"]["argv"][2:] == ["--now"]
    assert steps["studio_resume"]["argv"][2:] == ["--resume-only"]
    for name in ("studio_now", "studio_resume"):
        assert steps[name]["manual"] is True and not steps[name].get("skip_when_busy")
    # each step's flags are ones run_studio.py accepts (it refuses abbreviations)
    import run_studio

    assert run_studio._parse_args(steps["studio_now"]["argv"][2:]).now is True
    assert run_studio._parse_args(steps["studio_resume"]["argv"][2:]).resume_only is True
    plain = run_studio._parse_args(steps["studio"]["argv"][2:])
    assert not plain.now and not plain.resume_only


def test_the_scan_steps_have_their_own_lock_and_only_the_plain_one_runs_on_its_own():
    cfg = load_ops_config()
    steps = {s["name"]: s for s in cfg["steps"]}
    for name in ("studio_scan", "studio_scan_now"):
        # never waits for (or holds back) a studio session, which may run an hour
        assert steps[name]["lock"] == "studio_scan" and steps[name]["timeout_seconds"] == 0
        assert steps[name]["enabled"] is True and steps[name]["required"] is False
    assert steps["studio_scan"]["argv"] == ["python", "run_studio.py", "--scan"]
    assert not steps["studio_scan"].get("manual")
    assert steps["studio_scan_now"]["manual"] is True
    # right before the studio, so the day's piece is offered the day's topics
    order = [s["name"] for s in cfg["steps"]]
    assert order.index("score") < order.index("studio_scan") < order.index("studio")
    autos = cfg["auto_run_steps"]
    assert autos.index("studio_scan") + 1 == autos.index("studio")
    import run_studio

    assert run_studio._parse_args(steps["studio_scan"]["argv"][2:]).scan is True
    now = run_studio._parse_args(steps["studio_scan_now"]["argv"][2:])
    assert now.scan_now is True and not now.scan


def test_the_learning_steps_have_their_own_lock_and_only_the_plain_one_runs_on_its_own():
    cfg = load_ops_config()
    steps = {s["name"]: s for s in cfg["steps"]}
    for name in ("studio_learn", "studio_learn_now"):
        # never waits for (or holds back) a studio session, which may run an hour
        assert steps[name]["lock"] == "studio_learn" and steps[name]["timeout_seconds"] == 0
        assert steps[name]["enabled"] is True and steps[name]["required"] is False
    assert steps["studio_learn"]["argv"] == ["python", "run_studio.py", "--learn"]
    assert not steps["studio_learn"].get("manual")
    assert steps["studio_learn_now"]["manual"] is True
    # after the snapshot it learns from, in the automatic runs too
    order = [s["name"] for s in cfg["steps"]]
    assert order.index("feedback") < order.index("studio_learn")
    assert cfg["auto_run_steps"][-2:] == ["evolve", "studio_learn"]
    import run_studio

    assert run_studio._parse_args(steps["studio_learn"]["argv"][2:]).learn is True
    now = run_studio._parse_args(steps["studio_learn_now"]["argv"][2:])
    assert now.learn_now is True and not now.learn


def test_default_config_dry_run_lists_real_clis(capsys):
    """With the default config, --dry-run resolves every enabled step to an existing CLI."""
    rc = run_ops.main(["run", "--dry-run"])
    out = capsys.readouterr().out
    assert rc == 0
    names = []
    for line in out.splitlines():
        name, reason = line.split()[:2]
        names.append(name)
        if name in ("ingest", "score", "draft"):
            assert reason == "dry" and sys.executable in line
        if name in ("feedback", "evolve"):
            assert reason == "dry" and sys.executable in line  # on, and the CLI exists
    # A plain run leaves out the manual steps and the studio, which has a schedule entry of
    # its own (a session would hold the whole run, and the run lock, for an hour or more).
    assert names.index("score") < names.index("draft")
    assert "studio" not in names
    assert "studio_now" not in names and "studio_resume" not in names
    assert "draft_retry" not in names
    # the radar's scan and the learning step stay in it (each at most daily, its own lock)
    assert names.index("score") < names.index("studio_scan") < names.index("draft")
    assert names.index("feedback") < names.index("studio_learn")
    assert "studio_scan_now" not in names and "studio_learn_now" not in names
    assert run_ops.main(["run", "--dry-run", "--only", "studio"]) == 0
    [line] = capsys.readouterr().out.splitlines()
    assert line.split()[:2] == ["studio", "dry"] and line.endswith("run_studio.py")


def test_a_manual_studio_step_runs_when_named(capsys):
    assert run_ops.main(["run", "--dry-run", "--only", "studio_now,studio_resume"]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert [ln.split()[:2] for ln in lines] == [["studio_now", "dry"], ["studio_resume", "dry"]]
    assert lines[0].endswith("run_studio.py --now")
    assert lines[1].endswith("run_studio.py --resume-only")


def test_every_scheduler_gives_the_studio_its_own_entry_with_no_time_limit():
    """A plain run leaves the studio out, so each shipped schedule has a line of its own
    for it; the systemd unit has no start limit (a session runs an hour or more, each stage
    under its own limit in studio/config.yaml), which the plain run's unit keeps."""
    deploy = DEFAULT_OPS_CONFIG_PATH.parent.parent / "deploy"
    own = "run_ops.py run --only studio"

    def settings(unit: str) -> dict[str, str]:
        lines = (deploy / unit).read_text(encoding="utf-8").splitlines()
        return dict(ln.split("=", 1) for ln in lines if "=" in ln and not ln.startswith("#"))

    studio = settings("pipeline-studio.service")
    assert studio["ExecStart"].endswith(own) and studio["TimeoutStartSec"] == "infinity"
    assert settings("pipeline-studio.timer")["Unit"] == "pipeline-studio.service"
    assert settings("pipeline.service")["ExecStart"].endswith("run_ops.py run")
    cron = (deploy / "crontab.example").read_text(encoding="utf-8")
    assert [ln for ln in cron.splitlines() if own in ln and not ln.startswith("#")]
    for doc in (deploy / "README.md", DEFAULT_OPS_CONFIG_PATH.parent.parent / "HOWTO.md"):
        text = doc.read_text(encoding="utf-8")
        assert 'schtasks /Create /TN "pipeline-studio"' in text and own in text, doc.name
