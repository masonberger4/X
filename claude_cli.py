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


def build_argv(model: str, system: str, settings: dict[str, Any]) -> list[str]:
    """Print mode, JSON envelope, no tools, no session files, no project CLAUDE.md."""
    argv = [
        str(settings["binary"]),
        "-p",
        "--output-format",
        "json",
        "--bare",
        "--no-session-persistence",
        "--tools",
        "",
        "--model",
        model,
    ]
    if system:
        argv += ["--system-prompt", system]
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
) -> str:
    """The single subprocess call. The user prompt goes in on stdin (no arg-length limit)."""
    settings = cli_settings(cfg)
    argv = build_argv(model, system, settings)
    if shutil.which(argv[0]) is None:
        raise ClaudeCliError(
            f"{argv[0]!r} not found on PATH; install Claude Code and run `claude login`, "
            "or set models.backend: api"
        )
    log.debug("running %s (%d chars of prompt)", argv[0], len(user))
    try:
        proc = subprocess.run(
            argv,
            input=user,
            capture_output=True,
            text=True,
            timeout=float(settings["timeout_seconds"]),
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise ClaudeCliError(f"CLI timed out after {settings['timeout_seconds']}s") from exc
    except OSError as exc:
        raise ClaudeCliError(f"could not start {argv[0]!r}: {exc}") from exc
    if proc.returncode != 0:
        raise ClaudeCliError(
            f"CLI exited {proc.returncode}: {(proc.stderr or proc.stdout).strip()[:300]}"
        )
    return parse_envelope(proc.stdout)
