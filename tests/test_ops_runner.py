"""ops/runner.py with tiny real subprocesses (python -c ...) via sys.executable."""

import logging
import subprocess
import sys

import pytest

from ops import runner
from ops.runner import Step, StepResult, run_steps

PY = sys.executable


def py(code: str) -> list[str]:
    return ["python", "-c", code]


def test_success_captures_stdout_and_exit_code():
    res = run_steps([Step("hello", py("print('hi')"))])
    assert len(res) == 1
    r = res[0]
    assert r.name == "hello"
    assert r.argv[0] == PY  # "python" replaced by the running interpreter
    assert r.exit_code == 0
    assert r.ok and not r.failed and not r.timed_out and not r.skipped
    assert r.stdout_tail.strip() == "hi"
    assert r.finished_at >= r.started_at


def test_nonzero_exit_is_failed():
    res = run_steps([Step("bad", py("import sys; sys.stderr.write('boom'); sys.exit(3)"))])
    r = res[0]
    assert r.exit_code == 3
    assert r.failed and not r.ok
    assert "boom" in r.stderr_tail


def test_timeout_kills_and_flags():
    step = Step("slow", py("import time; time.sleep(30)"), timeout_seconds=1)
    res = run_steps([step])
    r = res[0]
    assert r.timed_out is True
    assert r.exit_code is None
    assert r.failed
    assert r.duration_seconds < 20


def test_required_failure_stops_later_steps():
    steps = [
        Step("a", py("import sys; sys.exit(1)"), required=True),
        Step("b", py("print('b')")),
        Step("c", py("print('c')"), required=True),
    ]
    res = run_steps(steps)
    assert [r.name for r in res] == ["a", "b", "c"]
    assert res[0].failed
    assert res[1].skipped_reason == runner.SKIP_UPSTREAM
    assert res[2].skipped_reason == runner.SKIP_UPSTREAM


def test_optional_failure_does_not_stop_later_steps():
    steps = [
        Step("a", py("import sys; sys.exit(1)"), required=False),
        Step("b", py("print('b')")),
    ]
    res = run_steps(steps)
    assert res[0].failed
    assert res[1].ok and res[1].stdout_tail.strip() == "b"


def test_missing_cli_file_is_skipped_not_merged(tmp_path, caplog):
    steps = [Step("feedback", ["python", "run_feedback_missing.py", "snapshot"], required=True)]
    with caplog.at_level(logging.WARNING, logger="ops.runner"):
        res = run_steps(steps, cwd=tmp_path)
    assert res[0].skipped_reason == runner.SKIP_NOT_MERGED
    assert any("not merged" in r.message for r in caplog.records)
    # A missing optional CLI is not "upstream failed" for later steps.
    steps.append(Step("after", py("print('ok')")))
    res = run_steps(steps, cwd=tmp_path)
    assert res[1].ok


def test_existing_cli_file_runs(tmp_path):
    script = tmp_path / "run_thing.py"
    script.write_text("print('ran')\n")
    res = run_steps([Step("thing", ["python", "run_thing.py"])], cwd=tmp_path)
    assert res[0].ok and res[0].stdout_tail.strip() == "ran"


def test_disabled_step_is_skipped():
    res = run_steps([Step("off", py("print(1)"), enabled=False)])
    assert res[0].skipped_reason == runner.SKIP_DISABLED


def test_only_selects_subset():
    steps = [Step("a", py("print('a')")), Step("b", py("print('b')"))]
    res = run_steps(steps, only=["b"])
    assert [r.name for r in res] == ["b"]


def test_dry_run_executes_no_subprocess(monkeypatch):
    def explode(*a, **kw):
        raise AssertionError("subprocess.run must not be called in dry run")

    monkeypatch.setattr(subprocess, "run", explode)
    res = run_steps([Step("a", py("print('a')"), required=True)], dry_run=True)
    assert res[0].skipped_reason == runner.SKIP_DRY_RUN
    assert res[0].argv[0] == PY


def test_tails_are_truncated():
    step = Step(
        "loud",
        py("import sys; sys.stdout.write('o'*500); sys.stderr.write('e'*500)"),
    )
    res = run_steps([step], tail_chars=100)
    assert len(res[0].stdout_tail) == 100
    assert len(res[0].stderr_tail) == 100


def test_env_is_merged_into_child(monkeypatch):
    monkeypatch.setenv("OPS_TEST_PARENT", "parent")
    res = run_steps(
        [Step("env", py("import os; print(os.environ['OPS_TEST_PARENT'], os.environ['OPS_X'])"))],
        env={"OPS_X": "child"},
    )
    assert res[0].stdout_tail.split() == ["parent", "child"]


def test_steps_from_config_and_defaults():
    cfg = {"steps": [{"name": "s", "argv": ["python", "x.py"]}]}
    steps = runner.steps_from_config(cfg)
    assert steps == [Step("s", ["python", "x.py"], True, False, 600)]


def test_step_result_properties():
    from datetime import UTC, datetime

    t = datetime(2026, 1, 1, tzinfo=UTC)
    assert StepResult("a", [], t, t, exit_code=0).ok
    assert StepResult("a", [], t, t, skipped_reason="x").skipped
    assert not StepResult("a", [], t, t, skipped_reason="x").failed


@pytest.mark.parametrize("argv,missing", [(["python", "-c", "x"], False), (["ls"], False)])
def test_cli_missing_only_for_py_files(tmp_path, argv, missing):
    assert runner.cli_missing(argv, tmp_path) is missing
