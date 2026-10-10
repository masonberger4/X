"""Shared fixtures.

Step 1: an in-memory Database. Step 2: a temp SQLite file created by step 1's Database
(so the real items/clusters/scores schema is used) and seeded with fake scored items.
"""

from datetime import UTC, datetime, timedelta

import pytest

import config
import run_app
import run_desktop
import run_feedback
import run_movers
import run_ops
import run_publish
import run_queue
from approval_queue import store
from db import Database
from ingest.base import Item

# Every module that loads .env at runtime. Tests must not see the developer's .env
# (DB path, keys, PUBLISH_ENABLED ...), so load_dotenv is a no-op under pytest.
_DOTENV_USERS = (
    config,
    run_app,
    run_desktop,
    run_feedback,
    run_movers,
    run_ops,
    run_publish,
    run_queue,
)
_ENV_FROM_DOTENV = ("DB_PATH", "PUBLISH_ENABLED")


@pytest.fixture(autouse=True)
def _isolate_from_dotenv(monkeypatch):
    for mod in _DOTENV_USERS:
        monkeypatch.setattr(mod, "load_dotenv", lambda *a, **k: False)
    for name in _ENV_FROM_DOTENV:
        monkeypatch.delenv(name, raising=False)


# Modules that test the CLI plumbing itself: they fake the process, not the launchers.
_CLI_TEST_MODULES = {"test_claude_cli", "test_claude_cli_session"}


@pytest.fixture
def cli_plumbing():
    """Ask for this to run the real claude_cli launchers (run_claude, run_session) with the
    process faked below them (shutil.which, subprocess.Popen); _never_spawn_the_cli then
    leaves them alone."""


@pytest.fixture(autouse=True)
def _never_spawn_the_cli(request, monkeypatch):
    """Every model call goes through the Claude Code CLI, which is on PATH on a developer's
    machine: a test that forgot to fake its call must fail loudly, not spend the account's
    usage. The CLI's own tests, and tests that ask for `cli_plumbing`, fake the process
    instead and are left alone."""
    import claude_cli

    module = request.module.__name__.rsplit(".", 1)[-1]
    if module in _CLI_TEST_MODULES or "cli_plumbing" in request.fixturenames:
        return

    def refuse(*a, **k):
        raise RuntimeError("a test tried to run the real Claude Code CLI; fake the call")

    monkeypatch.setattr(claude_cli, "run_claude", refuse)
    monkeypatch.setattr(claude_cli, "run_session", refuse)


@pytest.fixture(autouse=True)
def _no_automatic_runs(monkeypatch):
    """The shipped ops/config.yaml switches automatic runs on, and the desktop tests start a
    real uvicorn with the app's lifespan. No test may start the timer thread (it would run
    the pipeline at 06:00 in the middle of a test run); tests/test_autorun.py drives
    `tick()` directly and starts the thread through `_start_thread`."""
    from panel.autorun import AutoRunner

    monkeypatch.setattr(AutoRunner, "start", lambda self: None)


@pytest.fixture
def db():
    d = Database(":memory:")
    yield d
    d.close()


@pytest.fixture
def dedup_cfg():
    return {"title_similarity": 0.92, "near_dup_window_days": 14}


# ---- step 2 -----------------------------------------------------------------

URL = "https://doi.org/10.1000/xyz123"
ABSTRACT = (
    "In this phase 2 trial of 97 patients, the overall response rate was 88% and median "
    "PFS was 14.6 months."
)


def seed_item(
    conn,
    item_id,
    *,
    source="pubmed",
    total=8.5,
    hours_ago=1,
    url=URL,
    cluster_id=None,
    abstract=ABSTRACT,
):
    """Insert one item in its own (or a given) cluster with one score; return the cluster id."""
    now = datetime.now(UTC)
    if cluster_id is None:
        cur = conn.execute(
            "INSERT INTO clusters (title, norm_title, published_at, created_at, prefilter_status)"
            " VALUES (?, ?, ?, ?, 'pass')",
            (f"Title {item_id}", f"title {item_id}", now.isoformat(), now.isoformat()),
        )
        cluster_id = cur.lastrowid
    item = Item.build(source=source, url=url + "#" + item_id, title=f"Title {item_id}")
    conn.execute(
        """INSERT INTO items (id, source, url, doi, title, abstract, published_at, fetched_at,
                              dedup_hash, cluster_id, raw_json)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            item_id,
            source,
            url,
            None,
            f"Title {item_id}",
            abstract,
            now.isoformat(),
            now.isoformat(),
            item.dedup_hash,
            cluster_id,
            "{}",
        ),
    )
    scored_at = (now - timedelta(hours=hours_ago)).isoformat()
    conn.execute(
        """INSERT INTO scores (cluster_id, model, prompt_version, novelty, clinical_significance,
                               audience_interest, expertise_fit, timeliness, evidence_level,
                               hype_risk, total, rationale, suggested_angle, raw_response,
                               scored_at)
           VALUES (?, 'm', 'v1', 8, 9, 7, 9, 8, 'rct', 2, ?, ?, ?, '{}', ?)""",
        (cluster_id, total, f"rationale {item_id}", f"angle {item_id}", scored_at),
    )
    conn.commit()
    return cluster_id


@pytest.fixture
def db_file(tmp_path, monkeypatch):
    path = tmp_path / "pipeline.db"
    monkeypatch.setenv("DB_PATH", str(path))
    Database(str(path)).close()  # creates step 1's tables with the real schema
    return path


@pytest.fixture
def conn(db_file):
    c = store.connect(db_file)
    yield c
    c.close()


@pytest.fixture(autouse=True)
def _queue_template_globals():
    """Importing panel.app installs HAS_PANEL and friends on the approval queue's own Jinja
    environment for the rest of the process. Restore them around every test, so a test that
    sets them (`install_standalone_globals`, or the panel's own values) cannot change how a
    later test's queue pages render."""
    from approval_queue import app as queue_app

    before = dict(queue_app.templates.env.globals)
    yield
    queue_app.templates.env.globals.clear()
    queue_app.templates.env.globals.update(before)
