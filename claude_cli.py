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
import tempfile
from typing import Any

log = logging.getLogger(__name__)

API = "api"
CLAUDE_CODE = "claude_code"
BACKENDS = (API, CLAUDE_CODE)

DEFAULTS: dict[str, Any] = {
    "binary": "claude",
    "timeout_seconds": 300,
    "extra_args": [],
}


class ClaudeCliError(RuntimeError):
    """The CLI could not be run or did not return a usable result."""


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
) -> list[str]:
    """Print mode, JSON envelope, no tools, no session files. The system prompt travels
    in a file: it is long and full of quotes, and on Windows the argv goes through a
    .cmd wrapper where that is not safe. No `--bare`: it also skips the stored login
    ("Not logged in" on every call). The project's CLAUDE.md is kept out of the prompt
    by running the CLI from the temp directory instead (see run_claude)."""
    argv = [
        binary,
        "-p",
        "--output-format",
        "json",
        "--no-session-persistence",
        "--tools",
        "",
        "--model",
        model,
    ]
    if system_file:
        argv += ["--system-prompt-file", system_file]
    if effort:
        argv += ["--effort", effort]
    return argv + settings["extra_args"]


def parse_envelope(stdout: str) -> str:
    """Return the assistant text from `--output-format json` output."""
    try:
        data = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise ClaudeCliError(f"CLI output is not JSON: {stdout[:200]!r}") from exc
    if isinstance(data, list):  # stream-style array; the result event is last
        data = next((d for d in reversed(data) if d.get("type") == "result"), data[-1])
    if not isinstance(data, dict):
        raise ClaudeCliError(f"unexpected CLI output shape: {type(data).__name__}")
    subtype = data.get("subtype")
    if data.get("is_error") or subtype not in (None, "success"):
        # A failed API call arrives as is_error=true with subtype "success" and the
        # reason in terminal_reason (e.g. "api_error"); other failures set the subtype.
        reason = data.get("terminal_reason") if data.get("is_error") else None
        reason = reason if reason and reason != "success" else subtype or "error"
        raise ClaudeCliError(f"CLI reported {reason}: {str(data.get('result'))[:300]}")
    result = data.get("result")
    if not isinstance(result, str) or not result.strip():
        raise ClaudeCliError("CLI returned an empty result")
    return result


def run_claude(
    user: str,
    *,
    system: str = "",
    model: str,
    cfg: dict[str, Any] | None = None,
    effort: str | None = None,
) -> str:
    """The single subprocess call. The user prompt goes in on stdin (no arg-length limit).
    `effort` (low|medium|high|xhigh|max) maps to the CLI's --effort."""
    settings = cli_settings(cfg)
    binary = resolve_binary(settings)
    system_file = None
    if system:
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", suffix=".md", prefix="claude-system-", delete=False
        ) as fh:
            fh.write(system)
            system_file = fh.name
    argv = build_argv(binary, model, system_file, settings, effort)
    log.debug("running %s (%d chars of prompt)", binary, len(user))
    try:
        proc = subprocess.run(
            argv,
            input=user,
            capture_output=True,
            text=True,
            encoding="utf-8",
            cwd=tempfile.gettempdir(),  # not the repo: no CLAUDE.md auto-discovery
            timeout=float(settings["timeout_seconds"]),
            check=False,
        )
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
        raise ClaudeCliError(f"CLI exited {proc.returncode}: {_failure_reason(proc)}")
    return parse_envelope(proc.stdout)


def _failure_reason(proc: subprocess.CompletedProcess[str]) -> str:
    """The human-readable reason from a failed run: the envelope's `result` when stdout is
    the JSON envelope (its first 300 chars are all usage counters), else stderr/stdout."""
    out = (proc.stdout or "").strip()
    if out.startswith("{"):
        try:
            data = json.loads(out)
            result = str(data.get("result") or "").strip()
            reason = data.get("terminal_reason") or data.get("subtype") or "error"
            if result:
                return f"{reason}: {result[:300]}"
        except ValueError:
            pass
    return (proc.stderr or out).strip()[:300]
