"""Optional "headless" LLM backend: run the Claude Code CLI in print mode instead of
calling the Anthropic API directly.

Backend selection (`llm_backend()`): the `LLM_BACKEND` env var, else `models.backend`
in config.yaml, else "api". With "claude_code" the two Claude call sites
(`score/scorer.py:Scorer.create_message` and `draft/drafter.py:call_anthropic`) hand
their prompt to `run_claude`, which launches `claude -p` as a subprocess. The CLI is
logged in with your own Anthropic account, so usage counts against that account's
plan rather than a metered API key.

This is a convenience for personal, low-volume use. Trade-offs, documented in README:
no strict tool schema (the reply is validated in code and retried), the CLI must be
installed and logged in on the machine that runs cron, and the account's rolling
usage limits are shared with your own interactive sessions. The API backend stays
the default. `run_claude` is the only place that spawns the CLI; tests replace it
or `subprocess.run`.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

API = "api"
CLAUDE_CODE = "claude_code"
BACKENDS = (API, CLAUDE_CODE)

DEFAULTS: dict[str, Any] = {
    "binary": "claude",
    "timeout_seconds": 600,
    "extra_args": [],
}


class ClaudeCliError(RuntimeError):
    """The CLI could not be run or did not return a usable result."""


class ClaudeCliRefused(ClaudeCliError):
    """The CLI's usage-policy safeguard flagged the prompt. Deterministic for a given
    prompt, so retrying the same call is pointless; callers shrink the prompt instead."""


# Phrases the CLI's safeguard verdict carries (the API-side error text is the same).
REFUSAL_MARKERS = ("safeguards flagged", "anthropic.com/legal/aup")


def is_refusal(message: str) -> bool:
    text = message.lower()
    return any(marker in text for marker in REFUSAL_MARKERS)


class ClaudeCliUnavailable(ClaudeCliError):
    """The CLI is not installed or cannot start at all. Not worth retrying."""


def llm_backend(cfg: dict[str, Any] | None = None) -> str:
    """'api' (default) or 'claude_code'. LLM_BACKEND env wins over config.yaml models.backend."""
    env = os.environ.get("LLM_BACKEND", "").strip().lower()
    value = env or str(((cfg or {}).get("models") or {}).get("backend") or API).strip().lower()
    if value not in BACKENDS:
        raise ValueError(f"unknown LLM backend {value!r}; expected one of {BACKENDS}")
    return value


def cli_settings(cfg: dict[str, Any] | None = None) -> dict[str, Any]:
    out = dict(DEFAULTS)
    out.update((cfg or {}).get("claude_code") or {})
    out["extra_args"] = [str(a) for a in (out.get("extra_args") or [])]
    return out


def resolve_binary(settings: dict[str, Any]) -> str:
    """Full path of the CLI. On Windows npm installs `claude.cmd`; CreateProcess needs the
    resolved path (bare `claude` gives WinError 2 even though `which` finds it)."""
    binary = str(settings["binary"])
    found = shutil.which(binary)
    if found is None:
        raise ClaudeCliUnavailable(
            f"{binary!r} not found on PATH; install Claude Code and run `claude login`, "
            "or set models.backend: api"
        )
    return found


def build_argv(
    binary: str,
    model: str,
    system_file: str | None,
    settings: dict[str, Any],
    effort: str | None = None,
    tools: list[str] | None = None,
) -> list[str]:
    """Print mode, JSON envelope, no session files. `tools` are made available and
    pre-approved (empty by default, so no tools unless the caller passes some); the
    claim verifier is the one caller that does, with WebSearch/WebFetch. The system prompt travels
    in a file: it is long and full of quotes, and on Windows the argv goes through a
    .cmd wrapper where that is not safe. No `--bare`: it also skips the stored login
    ("Not logged in" on every call). The project's CLAUDE.md is kept out of the prompt
    by running the CLI from the temp directory instead (see run_claude). `tools` (only
    the claim verifier passes any: WebSearch/WebFetch) are both made available and
    pre-approved, since print mode cannot answer a permission prompt."""
    tool_list = ",".join(tools or [])
    argv = [
        binary,
        "-p",
        "--output-format",
        "json",
        "--verbose",  # the whole transcript as a list, so a lost final answer is recoverable
        "--no-session-persistence",
        "--tools",
        tool_list,
        "--model",
        model,
    ]
    if tool_list:
        argv += ["--allowedTools", tool_list]
    if system_file:
        argv += ["--system-prompt-file", system_file]
    if effort:
        argv += ["--effort", effort]
    return argv + settings["extra_args"]


def recover_answer(events: list[Any]) -> str:
    """The last non-empty assistant text in a --verbose transcript, else the input of
    the last tool call it made (serialised as JSON); empty when there is neither."""
    texts: list[str] = []
    tool_inputs: list[str] = []
    for event in events:
        if not isinstance(event, dict) or event.get("type") != "assistant":
            continue
        content = (event.get("message") or {}).get("content") or []
        for block in content if isinstance(content, list) else []:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text" and str(block.get("text") or "").strip():
                texts.append(block["text"])
            elif block.get("type") == "tool_use" and isinstance(block.get("input"), dict):
                tool_inputs.append(json.dumps(block["input"]))
    if texts:
        return texts[-1]
    return tool_inputs[-1] if tool_inputs else ""


def parse_envelope(stdout: str) -> str:
    """Return the assistant text from `--output-format json` output."""
    try:
        data = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise ClaudeCliError(f"CLI output is not JSON: {stdout[:200]!r}") from exc
    events: list[Any] = []
    if isinstance(data, list):  # --verbose: the transcript; the result event is last
        events = data
        data = next((d for d in reversed(data) if d.get("type") == "result"), data[-1])
    if not isinstance(data, dict):
        raise ClaudeCliError(f"unexpected CLI output shape: {type(data).__name__}")
    subtype = data.get("subtype")
    if data.get("is_error") or subtype not in (None, "success"):
        # A failed API call arrives as is_error=true with subtype "success" and the
        # reason in terminal_reason (e.g. "api_error"); other failures set the subtype.
        reason = data.get("terminal_reason") if data.get("is_error") else None
        reason = reason if reason and reason != "success" else subtype or "error"
        message = f"CLI reported {reason}: {str(data.get('result'))[:300]}"
        raise (ClaudeCliRefused if is_refusal(message) else ClaudeCliError)(message)
    result = data.get("result")
    if not isinstance(result, str) or not result.strip():
        # The model sometimes answers by calling the tool the prompt names; with no tools
        # the call fails and turn 2 ends empty, while the answer sits in turn 1.
        result = recover_answer(events)
    if not isinstance(result, str) or not result.strip():
        # Say why: a turn that ended on max_tokens (thinking used the budget) or a
        # refusal stop reads very differently from a plain empty reply.
        usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
        details = {
            "stop_reason": data.get("stop_reason"),
            "terminal_reason": data.get("terminal_reason"),
            "num_turns": data.get("num_turns"),
            "output_tokens": usage.get("output_tokens"),
        }
        shown = ", ".join(f"{k}={v}" for k, v in details.items() if v is not None)
        raise ClaudeCliError("CLI returned an empty result" + (f" ({shown})" if shown else ""))
    return result


def run_claude(
    user: str,
    *,
    system: str = "",
    model: str,
    cfg: dict[str, Any] | None = None,
    effort: str | None = None,
    tools: list[str] | None = None,
) -> str:
    """The single subprocess call. The user prompt goes in on stdin (no arg-length limit).
    `effort` (low|medium|high|xhigh|max) maps to the CLI's --effort; `tools` is empty for
    every caller except the claim verifier."""
    settings = cli_settings(cfg)
    binary = resolve_binary(settings)
    system_file = None
    if system:
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", suffix=".md", prefix="claude-system-", delete=False
        ) as fh:
            fh.write(system)
            system_file = fh.name
    argv = build_argv(binary, model, system_file, settings, effort, tools)
    log.debug("running %s (%d chars of prompt)", binary, len(user))
    try:
        proc = _run_with_timeout(argv, user, float(settings["timeout_seconds"]))
    except subprocess.TimeoutExpired as exc:
        raise ClaudeCliError(f"CLI timed out after {settings['timeout_seconds']}s") from exc
    except OSError as exc:
        raise ClaudeCliUnavailable(f"could not start {binary!r}: {exc}") from exc
    finally:
        if system_file:
            try:
                os.unlink(system_file)
            except OSError:
                pass
    if proc.returncode != 0:
        reason = f"CLI exited {proc.returncode}: {_failure_reason(proc)}"
        raise (ClaudeCliRefused if is_refusal(reason) else ClaudeCliError)(reason)
    return parse_envelope(proc.stdout)


def no_window_kwargs() -> dict[str, int]:
    """Extra Popen/run kwargs so a console child opens no window of its own on Windows.

    A windowed parent (pythonw, or Pipeline.exe from the desktop build) has no console, so
    Windows would otherwise create a visible one for every console program it launches:
    the operator saw a black "claude" window pop up and steal focus during a run. The
    child still gets a (hidden) console, so its own children inherit it. No-op elsewhere.
    """
    if sys.platform == "win32":
        return {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)}
    return {}


def _run_with_timeout(
    argv: list[str], stdin: str, timeout: float
) -> subprocess.CompletedProcess[str]:
    """subprocess.run with a timeout that actually ends the call. On Windows the npm
    `claude.cmd` wrapper starts node as a grandchild; killing only the wrapper leaves node
    holding the output pipes and the post-timeout communicate() blocks for good. Kill the
    whole tree (taskkill /T) before collecting output."""
    proc = subprocess.Popen(
        argv,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        cwd=tempfile.gettempdir(),  # not the repo: no CLAUDE.md auto-discovery
        **no_window_kwargs(),
    )
    try:
        out, err = proc.communicate(stdin, timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill_tree(proc)
        try:
            proc.communicate(timeout=10)
        except (subprocess.TimeoutExpired, OSError, ValueError):
            pass
        raise
    return subprocess.CompletedProcess(argv, proc.returncode, out, err)


def _kill_tree(proc: subprocess.Popen[str]) -> None:
    if sys.platform == "win32":
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
            capture_output=True,
            check=False,
            **no_window_kwargs(),
        )
    else:
        proc.kill()


def _failure_reason(proc: subprocess.CompletedProcess[str]) -> str:
    """The human-readable reason from a failed run: the envelope's `result` when stdout is
    the JSON envelope (its first 300 chars are all usage counters), else stderr/stdout."""
    out = (proc.stdout or "").strip()
    if out.startswith(("{", "[")):
        try:
            data = json.loads(out)
            if isinstance(data, list):  # --verbose transcript: the result event is last
                data = next((d for d in reversed(data) if d.get("type") == "result"), {})
            result = str(data.get("result") or "").strip()
            reason = data.get("terminal_reason") or data.get("subtype") or "error"
            if result:
                return f"{reason}: {result[:300]}"
        except (ValueError, AttributeError):
            pass
    return (proc.stderr or out).strip()[:300]


# --- long agentic sessions (the studio) ------------------------------------------------

# Credentials that would make the CLI bill a metered API key instead of the logged-in
# account. The app talks to Claude only through the CLI and its login, so they never reach
# the child process even when a .env still carries one.
_API_KEY_ENV = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")


def cli_env() -> dict[str, str]:
    """The environment a CLI child runs with: ours, minus any API credentials."""
    return {k: v for k, v in os.environ.items() if k not in _API_KEY_ENV}


@dataclass
class SessionResult:
    """How one agentic CLI invocation ended. `ok` is a clean finish; every other outcome
    keeps the session id so the caller can resume the same session."""

    session_id: str
    ok: bool
    subtype: str = ""  # success | error_max_turns | error_during_execution | killed | ...
    text: str = ""
    terminal_reason: str = ""
    num_turns: int = 0
    cost_usd: float = 0.0
    duration_ms: int = 0
    returncode: int | None = None
    stderr_tail: str = ""

    @property
    def detail(self) -> str:
        if self.ok:
            return "finished"
        reason = self.terminal_reason or self.subtype or "error"
        tail = " ".join((self.text or self.stderr_tail).split())[:300]
        return f"{reason}: {tail}" if tail else reason


def session_argv(
    binary: str,
    *,
    model: str,
    session_id: str,
    resume: bool,
    effort: str | None,
    tools: list[str],
    allowed: list[str],
    add_dirs: list[str],
    system_file: str | None,
    max_turns: int | None,
    flags: list[str],
) -> list[str]:
    """argv for one long agentic invocation. stream-json, so progress can be shown and
    stored as it happens; a fixed session id (new) or --resume (continuing), so a killed
    run can always be picked up again. Tools are made available with --tools and
    pre-approved with --allowedTools; `flags` carry the isolation switches (studio/config.yaml
    `cli_flags`: --safe-mode, --restricted, --permission-mode dontAsk). The prompt goes on
    stdin, since --tools, --allowedTools and --add-dir take any number of values."""
    argv = [binary, "-p", "--output-format", "stream-json", "--verbose", "--model", model]
    argv += ["--resume", session_id] if resume else ["--session-id", session_id]
    argv += ["--tools", ",".join(tools)]
    if allowed:
        argv += ["--allowedTools", ",".join(allowed)]
    for d in add_dirs:
        argv += ["--add-dir", d]
    if system_file:
        argv += ["--append-system-prompt-file", system_file]
    if effort:
        argv += ["--effort", effort]
    if max_turns:
        argv += ["--max-turns", str(int(max_turns))]
    return argv + [str(f) for f in flags]


def describe_event(event: dict[str, Any]) -> list[str]:
    """Short readable lines for one stream-json event (the session starting, each tool call,
    the assistant's text, the end), for the runs page and the studio's session log."""
    lines: list[str] = []
    etype = event.get("type")
    if etype == "system" and event.get("subtype") == "init":
        tools = ", ".join(str(t) for t in event.get("tools") or [])
        lines.append(f"session started ({event.get('model')}); tools: {tools}")
    elif etype == "assistant":
        message = event.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        for block in content if isinstance(content, list) else []:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_use":
                args = block.get("input") if isinstance(block.get("input"), dict) else {}
                lines.append(f"[{block.get('name')}] {_tool_summary(args)}".rstrip())
            elif block.get("type") == "text" and str(block.get("text") or "").strip():
                text = " ".join(str(block["text"]).split())
                lines.append(text[:240] + ("..." if len(text) > 240 else ""))
    elif etype == "result":
        lines.append(f"stage ended: {event.get('subtype')} after {event.get('num_turns')} turns")
    return lines


def _tool_summary(args: dict[str, Any]) -> str:
    for key in ("query", "url", "file_path", "pattern", "description", "prompt"):
        if args.get(key):
            value = " ".join(str(args[key]).split())
            return value[:160] + ("..." if len(value) > 160 else "")
    return ""


def run_session(
    prompt: str,
    *,
    model: str,
    cwd: str | os.PathLike[str],
    session_id: str,
    resume: bool = False,
    system: str = "",
    effort: str | None = None,
    tools: list[str] | None = None,
    allowed: list[str] | None = None,
    add_dirs: list[str] | None = None,
    flags: list[str] | None = None,
    max_turns: int | None = None,
    timeout: float | None = None,
    transcript: str | os.PathLike[str] | None = None,
    on_event: Callable[[dict[str, Any]], None] | None = None,
    cfg: dict[str, Any] | None = None,
) -> SessionResult:
    """One long agentic invocation of the CLI (a studio stage), run from `cwd` so the
    session's files and its stored conversation stay together. `system` is appended to
    Claude Code's own system prompt on the first invocation; a resume reuses what the
    session recorded. Every stream-json line is appended to `transcript` as it arrives and
    handed to `on_event`. `timeout` None or <= 0 means no limit; on expiry the CLI and what
    it started are killed and the result says so (the session can still be resumed).
    Raises ClaudeCliUnavailable when the CLI cannot start, and OSError when `transcript`
    cannot be opened (before anything starts); any other failure is returned."""
    settings = cli_settings(cfg)
    binary = resolve_binary(settings)
    system_file = None
    if system and not resume:
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", suffix=".md", prefix="claude-system-", delete=False
        ) as fh:
            fh.write(system)
            system_file = fh.name
    argv = session_argv(
        binary,
        model=model,
        session_id=session_id,
        resume=resume,
        effort=effort,
        tools=list(tools or []),
        allowed=list(allowed if allowed is not None else (tools or [])),
        add_dirs=[str(d) for d in (add_dirs or [])],
        system_file=system_file,
        max_turns=max_turns,
        flags=list(flags or []) + settings["extra_args"],
    )
    log.debug("starting session %s in %s", session_id, cwd)
    try:
        return _stream_session(argv, prompt, cwd, session_id, timeout, transcript, on_event)
    finally:
        if system_file:
            try:
                os.unlink(system_file)
            except OSError:
                pass


def _stream_session(
    argv: list[str],
    prompt: str,
    cwd: str | os.PathLike[str],
    session_id: str,
    timeout: float | None,
    transcript: str | os.PathLike[str] | None,
    on_event: Callable[[dict[str, Any]], None] | None,
) -> SessionResult:
    # Opened before the CLI starts: a transcript that cannot be written (its folder gone)
    # raises here, before there is a session left running with nobody reading it.
    out = open(transcript, "a", encoding="utf-8") if transcript else None  # noqa: SIM115
    try:
        proc = subprocess.Popen(
            argv,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            cwd=str(cwd),
            env=cli_env(),
            **no_window_kwargs(),
        )
    except OSError as exc:
        if out is not None:
            out.close()
        raise ClaudeCliUnavailable(f"could not start {argv[0]!r}: {exc}") from exc
    stderr_lines: list[str] = []
    finished = threading.Event()
    timed_out = threading.Event()

    def drain_stderr() -> None:
        assert proc.stderr is not None
        for line in proc.stderr:
            stderr_lines.append(line)
            del stderr_lines[:-200]

    def watchdog() -> None:
        if not finished.wait(timeout) and proc.poll() is None:
            timed_out.set()
            _kill_tree(proc)

    err_thread = threading.Thread(target=drain_stderr, daemon=True)
    err_thread.start()
    if timeout and timeout > 0:
        threading.Thread(target=watchdog, daemon=True).start()
    result_event: dict[str, Any] | None = None
    try:
        assert proc.stdin is not None and proc.stdout is not None
        try:
            proc.stdin.write(prompt)
            proc.stdin.close()
        except OSError:
            pass  # the CLI died at start-up; its exit code and stderr say why
        for line in proc.stdout:
            if out is not None:
                out.write(line)
                out.flush()
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(event, dict):
                continue
            if event.get("type") == "result":
                result_event = event
            if on_event is not None:
                try:
                    on_event(event)
                except Exception:  # a display hook must never end the session
                    log.debug("on_event failed", exc_info=True)
        proc.wait()
    except BaseException:
        _kill_tree(proc)
        raise
    finally:
        finished.set()
        if out is not None:
            out.close()
        err_thread.join(timeout=5)
    stderr_tail = "".join(stderr_lines)[-2000:]
    if result_event is None or timed_out.is_set():
        # Killed at the time limit even after a result line (the CLI starts another turn
        # when a background sub-agent ends): the stage stopped early and can be resumed.
        last = result_event or {}
        return SessionResult(
            session_id=str(last.get("session_id") or session_id),
            ok=False,
            subtype="killed" if timed_out.is_set() else "no_result",
            terminal_reason="timed out" if timed_out.is_set() else f"CLI exited {proc.returncode}",
            num_turns=int(last.get("num_turns") or 0),
            cost_usd=float(last.get("total_cost_usd") or 0.0),
            duration_ms=int(last.get("duration_ms") or 0),
            returncode=proc.returncode,
            stderr_tail=stderr_tail,
        )
    subtype = str(result_event.get("subtype") or "")
    reason = str(result_event.get("terminal_reason") or "")
    text = str(result_event.get("result") or "")
    ok = subtype == "success" and not result_event.get("is_error") and proc.returncode == 0
    if not ok and is_refusal(f"{reason} {text}"):
        subtype = "refused"
    if reason in ("", "completed", "success"):
        # A result line that reads as a clean finish from a CLI that then exited non-zero:
        # the exit is the reason, not "success".
        reason = "" if ok or proc.returncode in (0, None) else f"CLI exited {proc.returncode}"
    return SessionResult(
        session_id=str(result_event.get("session_id") or session_id),
        ok=ok,
        subtype=subtype,
        text=text,
        terminal_reason=reason,
        num_turns=int(result_event.get("num_turns") or 0),
        cost_usd=float(result_event.get("total_cost_usd") or 0.0),
        duration_ms=int(result_event.get("duration_ms") or 0),
        returncode=proc.returncode,
        stderr_tail=stderr_tail,
    )


def session_started(transcript: str | os.PathLike[str], session_id: str) -> bool:
    """Whether the CLI ever started `session_id` (its init event is in the transcript), so
    the next invocation must --resume it rather than create it."""
    path = Path(transcript)
    if not path.is_file():
        return False
    needle = f'"session_id":"{session_id}"'
    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            if '"init"' in line and needle in line.replace(" ", ""):
                return True
    return False
