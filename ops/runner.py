"""Run the pipeline's CLIs as subprocesses, in order, with per-step timeouts.

Pure with respect to the database: the caller records the returned StepResults.
Other steps' modules are never imported; a CLI file missing from this checkout
(a step that has not merged yet) is skipped with a WARNING.
"""

from __future__ import annotations

import logging
import os
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

SKIP_DISABLED = "disabled"
SKIP_NOT_MERGED = "not merged"
SKIP_UPSTREAM = "upstream failed"
SKIP_DRY_RUN = "dry run"
SKIP_CANCELLED = "cancelled"

# Live children of this process, so a caller (the control panel's Stop button, or the
# desktop app closing) can end a run: each step's process and everything it launched.
_ACTIVE: set[subprocess.Popen] = set()
_ACTIVE_LOCK = threading.Lock()
_STOP = threading.Event()


@dataclass
class Step:
    name: str
    argv: list[str]
    enabled: bool = True
    required: bool = False
    timeout_seconds: int = 600  # 0 = no limit: the step runs until it exits or is stopped

    @property
    def wait_timeout(self) -> float | None:
        return None if self.timeout_seconds <= 0 else float(self.timeout_seconds)

    @classmethod
    def from_config(cls, raw: dict[str, Any]) -> Step:
        return cls(
            name=str(raw["name"]),
            argv=[str(a) for a in raw["argv"]],
            enabled=bool(raw.get("enabled", True)),
            required=bool(raw.get("required", False)),
            timeout_seconds=int(raw.get("timeout_seconds", 600)),
        )


def steps_from_config(cfg: dict[str, Any]) -> list[Step]:
    return [Step.from_config(s) for s in cfg.get("steps") or []]


@dataclass
class StepResult:
    name: str
    argv: list[str]
    started_at: datetime
    finished_at: datetime
    exit_code: int | None = None
    timed_out: bool = False
    skipped_reason: str | None = None
    stdout_tail: str = ""
    stderr_tail: str = ""
    extra: dict[str, Any] = field(default_factory=dict, repr=False)

    @property
    def skipped(self) -> bool:
        return self.skipped_reason is not None

    @property
    def ok(self) -> bool:
        return not self.skipped and not self.timed_out and self.exit_code == 0

    @property
    def failed(self) -> bool:
        return not self.skipped and not self.ok

    @property
    def duration_seconds(self) -> float:
        return (self.finished_at - self.started_at).total_seconds()


def _now() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def _tail(text: str | bytes | None, n: int) -> str:
    if text is None:
        return ""
    if isinstance(text, bytes):
        text = text.decode("utf-8", errors="replace")
    return text[-n:] if n >= 0 else text


def resolve_argv(argv: list[str], python: str) -> list[str]:
    out = list(argv)
    if out and out[0] == "python":
        out[0] = python
    return out


def cli_missing(argv: list[str], cwd: Path) -> bool:
    """True when argv names a .py CLI that does not exist on this checkout.

    In a PyInstaller build the scripts are shipped inside the bundle rather than the
    working directory, so that folder is checked too (it is only set when frozen).
    """
    if len(argv) < 2 or not argv[1].endswith(".py"):
        return False
    script = Path(argv[1])
    if script.is_absolute():
        return not script.exists()
    candidates = [cwd / script]
    bundle = getattr(sys, "_MEIPASS", None)
    if bundle:
        candidates.append(Path(bundle) / script)
    return not any(c.exists() for c in candidates)


def run_steps(
    steps: Iterable[Step],
    *,
    only: Iterable[str] | None = None,
    dry_run: bool = False,
    env: dict[str, str] | None = None,
    python: str = sys.executable,
    cwd: str | os.PathLike[str] | None = None,
    tail_chars: int = 4000,
    on_start: Callable[[Step], None] | None = None,
    on_result: Callable[[StepResult], None] | None = None,
    on_output: Callable[[Step, str, str], None] | None = None,
) -> list[StepResult]:
    """Run each enabled step in order. See module docstring for the skip/stop rules.

    `on_start` is called just before a step's process is launched and `on_result` with
    every StepResult as it is produced (run, skipped or timed out), so a caller watching
    the run (the control panel) can show progress before the whole list returns.
    `on_output` is called from the reader threads with the step and the current
    stdout/stderr tails every time the live process writes something, so a watcher can
    show a step's log while it runs rather than when it ends. It must be cheap and
    thread-safe.
    """
    workdir = Path(cwd) if cwd else Path.cwd()
    selected = set(only) if only is not None else None
    # Python children block-buffer stdout on a pipe, so without this a step's log would
    # arrive in one lump at exit whatever the reader does.
    child_env = {**os.environ, "PYTHONUNBUFFERED": "1", **(env or {})}
    results: list[StepResult] = []
    upstream_failed = False
    _STOP.clear()

    def add(result: StepResult) -> None:
        results.append(result)
        if on_result is not None:
            on_result(result)

    for step in steps:
        if selected is not None and step.name not in selected:
            continue
        argv = resolve_argv(step.argv, python)
        now = _now()
        if _STOP.is_set():
            log.warning("step %s: skipped, the run was stopped", step.name)
            add(StepResult(step.name, argv, now, now, skipped_reason=SKIP_CANCELLED))
            continue
        if not step.enabled:
            log.info("step %s: disabled, skipping", step.name)
            add(StepResult(step.name, argv, now, now, skipped_reason=SKIP_DISABLED))
            continue
        if upstream_failed:
            log.warning("step %s: skipped because a required upstream step failed", step.name)
            add(StepResult(step.name, argv, now, now, skipped_reason=SKIP_UPSTREAM))
            continue
        if cli_missing(argv, workdir):
            log.warning(
                "step %s: %s not found on this checkout (not merged); skipping", step.name, argv[1]
            )
            add(StepResult(step.name, argv, now, now, skipped_reason=SKIP_NOT_MERGED))
            continue
        if dry_run:
            log.info(
                "step %s: would run %s (timeout %s)",
                step.name,
                argv,
                "none" if step.wait_timeout is None else f"{step.timeout_seconds}s",
            )
            add(StepResult(step.name, argv, now, now, skipped_reason=SKIP_DRY_RUN))
            continue

        if on_start is not None:
            on_start(step)
        result = _run_one(step, argv, workdir, child_env, tail_chars, on_output=on_output)
        add(result)
        if result.failed and step.required:
            upstream_failed = True
    return results


def no_window_kwargs() -> dict[str, int]:
    """Extra Popen/run kwargs so a console child opens no window of its own on Windows.

    A windowed parent (pythonw, or Pipeline.exe from the desktop build) has no console, so
    Windows would otherwise create a visible one for every console program it launches:
    the operator saw a black "claude" window pop up and steal focus during a run. The
    child still gets a (hidden) console, so its own children inherit it. No-op elsewhere.
    (Also in claude_cli.py: ops never imports another step's module.)
    """
    if sys.platform == "win32":
        return {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)}
    return {}


def _own_group_kwargs() -> dict[str, bool]:
    """Start each step in its own process group (POSIX) so killing it takes its children
    (the claude CLI, for one) with it. Windows uses taskkill /T for the same effect."""
    return {} if sys.platform == "win32" else {"start_new_session": True}


def _kill_tree(proc: subprocess.Popen) -> None:
    """End a step and everything it launched."""
    if proc.poll() is not None:
        return
    if sys.platform == "win32":
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
            capture_output=True,
            check=False,
            **no_window_kwargs(),
        )
    else:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            proc.kill()


def terminate_active() -> int:
    """Stop the run in progress: kill every live step (and its children) and make
    run_steps skip whatever steps remain. Returns how many processes were ended."""
    _STOP.set()
    with _ACTIVE_LOCK:
        procs = list(_ACTIVE)
    for proc in procs:
        log.warning("stopping step process %s", proc.pid)
        _kill_tree(proc)
    return len(procs)


def _pump(
    stream: Any,
    buf: list[str],
    lock: threading.Lock,
    notify: Callable[[], None] | None,
) -> None:
    """Copy a child's pipe into `buf` chunk by chunk, telling the watcher each time."""
    try:
        while True:
            chunk = stream.readline()
            if not chunk:
                break
            with lock:
                buf.append(chunk)
            if notify is not None:
                notify()
    except (OSError, ValueError):  # pipe closed under us (a killed step)
        pass
    finally:
        try:
            stream.close()
        except OSError:
            pass


def _run_one(
    step: Step,
    argv: list[str],
    cwd: Path,
    env: dict[str, str] | None,
    tail_chars: int,
    on_output: Callable[[Step, str, str], None] | None = None,
) -> StepResult:
    started = _now()
    t0 = time.monotonic()
    log.info("step %s: starting %s", step.name, argv)
    timed_out = False
    exit_code: int | None = None
    out_buf: list[str] = []
    err_buf: list[str] = []
    buf_lock = threading.Lock()

    def tails() -> tuple[str, str]:
        with buf_lock:
            return _tail("".join(out_buf), tail_chars), _tail("".join(err_buf), tail_chars)

    def notify() -> None:
        if on_output is not None:
            out, err = tails()
            on_output(step, out, err)

    try:
        proc = subprocess.Popen(
            argv,
            cwd=str(cwd),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            errors="replace",
            **_own_group_kwargs(),
            **no_window_kwargs(),
        )
    except OSError as exc:  # interpreter/script not executable etc.
        exit_code = 127
        err_buf.append(f"{type(exc).__name__}: {exc}")
    else:
        with _ACTIVE_LOCK:
            _ACTIVE.add(proc)
            # A stop that landed between on_start and Popen found nothing to
            # kill; end the process here, not after its whole run.
            stopped_early = _STOP.is_set()
        if stopped_early:
            _kill_tree(proc)
        # One reader per pipe: the process is never blocked on a full pipe, and the
        # watcher sees each line as it is written instead of everything at exit.
        readers = [
            threading.Thread(target=_pump, args=(stream, buf, buf_lock, notify), daemon=True)
            for stream, buf in ((proc.stdout, out_buf), (proc.stderr, err_buf))
            if stream is not None
        ]
        for t in readers:
            t.start()
        try:
            try:
                proc.wait(timeout=step.wait_timeout)
            except subprocess.TimeoutExpired:
                timed_out = True
                _kill_tree(proc)
                proc.wait()
        finally:
            for t in readers:
                t.join()
            with _ACTIVE_LOCK:
                _ACTIVE.discard(proc)
        # A timed-out step records no exit code (it was killed); the status page shows
        # "timed out" instead. A stopped run's step keeps its (kill) code: it did fail.
        exit_code = None if timed_out else proc.returncode
    stdout, stderr = tails()
    finished = _now()
    duration = time.monotonic() - t0
    result = StepResult(
        name=step.name,
        argv=argv,
        started_at=started,
        finished_at=finished,
        exit_code=exit_code,
        timed_out=timed_out,
        stdout_tail=stdout,
        stderr_tail=stderr,
    )
    if timed_out:
        log.error("step %s: timed out after %ss (killed)", step.name, step.timeout_seconds)
    elif result.ok:
        log.info("step %s: finished in %.1fs, exit 0", step.name, duration)
    else:
        log.error("step %s: failed in %.1fs, exit %s", step.name, duration, exit_code)
        if result.stderr_tail:
            log.error("step %s stderr tail:\n%s", step.name, result.stderr_tail[-1000:])
    return result
