"""ops/runner.py with tiny real subprocesses (python -c ...) via sys.executable."""

import logging
import subprocess
import sys
import time

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


def test_timeout_zero_means_no_limit():
    step = Step("quick", py("import time; time.sleep(0.2)"), timeout_seconds=0)
    assert step.wait_timeout is None
    r = run_steps([step])[0]
    assert r.ok and not r.timed_out
    assert (
        Step.from_config(
            {"name": "v", "argv": ["python", "x.py"], "timeout_seconds": 0}
        ).wait_timeout
        is None
    )


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


def test_steps_are_launched_without_a_console_window(monkeypatch, tmp_path):
    seen = {}

    class Done:
        pid = 1
        returncode = 0
        stdout = None
        stderr = None

        def wait(self, timeout=None):
            return 0

        def poll(self):
            return 0

    def fake_popen(argv, **kwargs):
        seen["kwargs"] = kwargs
        return Done()

    monkeypatch.setattr(runner, "no_window_kwargs", lambda: {"creationflags": 7})
    monkeypatch.setattr(runner.subprocess, "Popen", fake_popen)
    step = runner.Step("s", ["python", "-c", "pass"])
    result = runner._run_one(step, ["python", "-c", "pass"], tmp_path, None, 100)
    assert result.ok and seen["kwargs"]["creationflags"] == 7


def test_no_window_kwargs_only_on_windows(monkeypatch):
    monkeypatch.setattr(runner.sys, "platform", "linux")
    assert runner.no_window_kwargs() == {}
    monkeypatch.setattr(runner.sys, "platform", "win32")
    monkeypatch.setattr(runner.subprocess, "CREATE_NO_WINDOW", 0x08000000, raising=False)
    assert runner.no_window_kwargs() == {"creationflags": 0x08000000}


def test_terminate_active_kills_a_running_step_and_its_children(tmp_path):
    """A step that spawns a grandchild (as a step spawning the claude CLI does) is ended
    as a tree, and the steps after it are skipped as cancelled."""
    import threading
    import time

    marker = tmp_path / "grandchild.pid"
    code = (
        "import subprocess, sys, time, pathlib\n"
        f"p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
        f"pathlib.Path({str(marker)!r}).write_text(str(p.pid))\n"
        "time.sleep(60)\n"
    )
    steps = [
        Step("slow", [sys.executable, "-c", code], timeout_seconds=60),
        Step("after", [sys.executable, "-c", "print('should not run')"]),
    ]
    results: list = []
    t = threading.Thread(target=lambda: results.extend(run_steps(steps, cwd=tmp_path)))
    t.start()
    deadline = time.monotonic() + 10
    while not marker.exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert marker.exists(), "the step never started its grandchild"
    grandchild = int(marker.read_text())

    assert runner.terminate_active() == 1
    t.join(timeout=10)
    assert not t.is_alive()
    assert results[0].name == "slow" and results[0].failed and not results[0].timed_out
    assert results[1].skipped_reason == runner.SKIP_CANCELLED

    from ops.lock import pid_alive  # works on Windows too (os.kill(pid, 0) does not)

    deadline = time.monotonic() + 5
    while pid_alive(grandchild) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert not pid_alive(grandchild), "the grandchild survived terminate_active"


def test_terminate_active_with_nothing_running_is_harmless():
    assert runner.terminate_active() == 0


def test_a_new_run_clears_a_previous_stop(tmp_path):
    runner.terminate_active()
    (res,) = run_steps([Step("ok", [sys.executable, "-c", "print(1)"])], cwd=tmp_path)
    assert res.ok


def test_callbacks_report_each_step_as_it_happens():
    """The control panel shows progress mid-run through these hooks."""
    started, finished = [], []
    steps = [
        Step("a", py("print('a')")),
        Step("off", py("print('x')"), enabled=False),
        Step("b", py("print('b')")),
    ]
    res = run_steps(steps, on_start=started.append, on_result=finished.append)
    assert [s.name for s in started] == ["a", "b"], "skipped steps never start"
    assert [r.name for r in finished] == ["a", "off", "b"]
    assert finished == res


def test_a_stop_between_on_start_and_launch_still_ends_the_step():
    """The panel reports a step as live from on_start, so a Stop can arrive before
    the process exists; the step must be killed on launch, not run to completion."""
    step = Step("slow", py("import time; time.sleep(30)"), timeout_seconds=60)
    t0 = time.monotonic()
    res = run_steps([step], on_start=lambda _s: runner.terminate_active())
    assert time.monotonic() - t0 < 20
    assert res[0].name == "slow" and not res[0].ok and not res[0].timed_out


def test_output_is_reported_while_a_step_runs(tmp_path):
    """The watcher sees the first line before the step exits, not one lump at the end."""
    import threading

    seen: list[str] = []
    first_line = threading.Event()
    gate = tmp_path / "go"

    def output(step, out, err):
        seen.append(out)
        if "one" in out:
            first_line.set()

    code = (
        "import sys, time, pathlib; print('one'); sys.stdout.flush(); "
        f"p = pathlib.Path({str(gate)!r})\n"
        "while not p.exists(): time.sleep(0.02)\n"
        "print('two')"
    )
    step = runner.Step("s", ["python", "-c", code], timeout_seconds=20)

    def release():
        assert first_line.wait(10), "the first line never reached the watcher"
        gate.write_text("")

    t = threading.Thread(target=release)
    t.start()
    res = run_steps([step], on_output=output)
    t.join()
    assert res[0].ok and res[0].stdout_tail.split() == ["one", "two"]
    assert seen[0].split() == ["one"] and seen[-1].split() == ["one", "two"]


def test_children_get_unbuffered_python_output(tmp_path):
    """No flush in the child: the line still lands as it is printed (PYTHONUNBUFFERED)."""
    step = runner.Step("s", ["python", "-c", "import os; print(os.environ['PYTHONUNBUFFERED'])"])
    res = run_steps([step], env={"OTHER": "1"})
    assert res[0].ok and res[0].stdout_tail.strip() == "1"
