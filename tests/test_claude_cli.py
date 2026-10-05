"""The Claude Code CLI (claude_cli), the app's only way to reach Claude, and its use by the
scorer and drafter. The CLI is never spawned: run_claude, _run_with_timeout or Popen are
replaced (the timeout and environment tests start a short Python child instead)."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tomllib
from pathlib import Path
from types import SimpleNamespace

import pytest

import claude_cli
from draft import drafter
from score import rubric
from score.scorer import HeadlessResponse, Scorer, ScoringError

ROOT = Path(__file__).resolve().parents[1]
API_CREDENTIALS = {"ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"}

# ---- the CLI is the only way to Claude -------------------------------------------


def test_shipped_config_runs_the_logged_in_cli():
    import config

    cfg = config.load_config()
    assert claude_cli.cli_settings(cfg)["binary"] == "claude"
    assert "backend" not in cfg["models"]  # no switch left that could point at the API


def test_no_app_module_imports_the_anthropic_sdk():
    """Not a dependency, and not imported anywhere, not even lazily inside a function."""
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    extras = project.get("optional-dependencies", {}).values()
    requirements = [r for group in [project["dependencies"], *extras] for r in group]
    assert not [r for r in requirements if re.match(r"anthropic(?![\w.-])", r, re.I)]

    modules = list(ROOT.glob("*.py"))
    for package in sorted(ROOT.iterdir()):
        if package.name != "tests" and (package / "__init__.py").is_file():
            modules += package.rglob("*.py")
    sdk_import = re.compile(r"^\s*(?:import|from)\s+anthropic\b", re.M)
    offenders = [
        str(path.relative_to(ROOT))
        for path in modules
        if sdk_import.search(path.read_text(encoding="utf-8"))
    ]
    assert len(modules) > 50 and offenders == []


# ---- the child's environment ------------------------------------------------------


def test_cli_env_strips_api_credentials_and_keeps_the_rest(monkeypatch):
    for name in API_CREDENTIALS:
        monkeypatch.delenv(name, raising=False)
    assert claude_cli.cli_env() == dict(os.environ)  # nothing to strip: an exact copy

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-api-stale")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "stale-gateway-token")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "account-login")  # the CLI's own login
    monkeypatch.setenv("PIPELINE_TEST_VAR", "kept")
    env = claude_cli.cli_env()
    assert set(os.environ) - set(env) == API_CREDENTIALS
    assert all(env[name] == os.environ[name] for name in env)  # the rest is untouched
    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == "account-login"
    assert env["PIPELINE_TEST_VAR"] == "kept" and env["PATH"] == os.environ["PATH"]
    assert os.environ["ANTHROPIC_API_KEY"] == "sk-ant-api-stale"  # a copy: ours is unchanged


def test_run_claude_hands_popen_the_env_without_api_credentials(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-api-stale")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "stale-gateway-token")
    seen = {}

    class FakeProc:
        pid = 1
        returncode = 0

        def communicate(self, stdin, timeout=None):
            seen["stdin"] = stdin
            return json.dumps({"subtype": "success", "result": "answer"}), ""

    def fake_popen(argv, **kwargs):
        seen["argv"], seen["kwargs"] = argv, kwargs
        return FakeProc()

    monkeypatch.setattr(claude_cli.shutil, "which", lambda b: "/usr/bin/" + b)
    monkeypatch.setattr(claude_cli.subprocess, "Popen", fake_popen)
    assert claude_cli.run_claude("USER", model="m") == "answer"
    assert seen["argv"][0] == "/usr/bin/claude" and seen["stdin"] == "USER"
    env = seen["kwargs"]["env"]
    assert not API_CREDENTIALS & set(env)
    assert env == claude_cli.cli_env() and env["PATH"] == os.environ["PATH"]


def test_the_cli_child_never_sees_an_api_key(monkeypatch):
    """A real child process: whatever an old .env put in our environment, the CLI runs on
    its own login and never bills a metered API key."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-api-stale")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "stale-gateway-token")
    monkeypatch.setenv("PIPELINE_TEST_VAR", "kept")
    names = sorted(API_CREDENTIALS | {"PIPELINE_TEST_VAR"})
    code = f"import json, os; print(json.dumps({{k: os.environ.get(k) for k in {names!r}}}))"
    proc = claude_cli._run_with_timeout([sys.executable, "-c", code], "", timeout=30)
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout) == {
        "ANTHROPIC_API_KEY": None,
        "ANTHROPIC_AUTH_TOKEN": None,
        "PIPELINE_TEST_VAR": "kept",
    }


# ---- argv and envelope ----------------------------------------------------------


def test_build_argv_is_print_mode_without_tools():
    argv = claude_cli.build_argv(
        "C:\\npm\\claude.cmd",
        "claude-haiku-4-5",
        "sys.md",
        claude_cli.cli_settings({"claude_code": {"extra_args": ["-x"]}}),
    )
    assert argv[:2] == ["C:\\npm\\claude.cmd", "-p"]
    assert argv[argv.index("--output-format") + 1] == "json"
    assert argv[argv.index("--tools") + 1] == ""
    assert argv[argv.index("--model") + 1] == "claude-haiku-4-5"
    assert argv[argv.index("--system-prompt-file") + 1] == "sys.md"
    assert "--system-prompt" not in argv  # long prompt never goes on the command line
    assert "--no-session-persistence" in argv
    assert "--bare" not in argv  # --bare skips the stored login: "Not logged in"
    assert argv[-1] == "-x"


def test_parse_envelope_success_error_and_garbage():
    ok = json.dumps({"type": "result", "subtype": "success", "is_error": False, "result": "hi"})
    assert claude_cli.parse_envelope(ok) == "hi"
    as_list = json.dumps([{"type": "system"}, json.loads(ok)])
    assert claude_cli.parse_envelope(as_list) == "hi"
    with pytest.raises(claude_cli.ClaudeCliError, match="error_max_turns"):
        claude_cli.parse_envelope(json.dumps({"subtype": "error_max_turns", "result": "x"}))
    auth_fail = {
        "subtype": "success",
        "is_error": True,
        "terminal_reason": "api_error",
        "result": "Authentication error",
    }  # real shape from `claude -p` when not logged in
    with pytest.raises(claude_cli.ClaudeCliError, match="api_error: Authentication error"):
        claude_cli.parse_envelope(json.dumps(auth_fail))
    with pytest.raises(claude_cli.ClaudeCliError, match="empty"):
        claude_cli.parse_envelope(json.dumps({"subtype": "success", "result": ""}))
    with pytest.raises(claude_cli.ClaudeCliError, match="not JSON"):
        claude_cli.parse_envelope("Welcome to Claude Code")


def test_run_claude_pipes_prompt_on_stdin(monkeypatch):
    seen = {}

    def fake_run(argv, stdin, timeout):
        seen["argv"], seen["stdin"], seen["timeout"] = argv, stdin, timeout
        with open(argv[argv.index("--system-prompt-file") + 1], encoding="utf-8") as fh:
            seen["system_text"] = fh.read()
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps({"subtype": "success", "result": "answer"}),
            stderr="",
        )

    monkeypatch.setattr(claude_cli.shutil, "which", lambda b: "C:\\npm\\" + b + ".CMD")
    monkeypatch.setattr(claude_cli, "_run_with_timeout", fake_run)
    out = claude_cli.run_claude(
        "USER", system="SYS", model="m", cfg={"claude_code": {"timeout_seconds": 7}}
    )
    assert out == "answer"
    assert seen["argv"][0] == "C:\\npm\\claude.CMD"  # resolved path, not the bare name
    assert seen["stdin"] == "USER"
    assert seen["timeout"] == 7.0
    assert "USER" not in seen["argv"] and "SYS" not in seen["argv"]
    system_file = seen["argv"][seen["argv"].index("--system-prompt-file") + 1]
    assert seen["system_text"] == "SYS"
    assert not os.path.exists(system_file)  # temp file removed after the call


def test_run_claude_failures_become_cli_errors(monkeypatch):
    monkeypatch.setattr(claude_cli.shutil, "which", lambda b: None)
    with pytest.raises(claude_cli.ClaudeCliUnavailable, match="not found") as missing:
        claude_cli.run_claude("u", model="m")
    assert "claude login" in str(missing.value)  # names the fix...
    assert "backend" not in str(missing.value)  # ...and no API fallback to switch to

    monkeypatch.setattr(claude_cli.shutil, "which", lambda b: b)

    def winerror2(*a, **k):
        raise FileNotFoundError(2, "The system cannot find the file specified")

    monkeypatch.setattr(claude_cli, "_run_with_timeout", winerror2)
    with pytest.raises(claude_cli.ClaudeCliUnavailable, match="could not start"):
        claude_cli.run_claude("u", model="m")

    monkeypatch.setattr(claude_cli.shutil, "which", lambda b: b)
    monkeypatch.setattr(
        claude_cli,
        "_run_with_timeout",
        lambda *a, **k: SimpleNamespace(returncode=1, stdout="", stderr="Not logged in"),
    )
    with pytest.raises(claude_cli.ClaudeCliError, match="exited 1: Not logged in"):
        claude_cli.run_claude("u", model="m")

    # non-zero exit with the JSON envelope on stdout: surface its `result`, not the counters
    envelope = json.dumps(
        {
            "usage": {"x": 0},
            "is_error": True,
            "terminal_reason": "api_error",
            "subtype": "success",
            "result": "Model not available on this plan",
        }
    )
    monkeypatch.setattr(
        claude_cli,
        "_run_with_timeout",
        lambda *a, **k: SimpleNamespace(returncode=1, stdout=envelope, stderr=""),
    )
    with pytest.raises(claude_cli.ClaudeCliError, match="api_error: Model not available"):
        claude_cli.run_claude("u", model="m")

    def timeout(*a, **k):
        raise subprocess.TimeoutExpired(cmd="claude", timeout=1)

    monkeypatch.setattr(claude_cli, "_run_with_timeout", timeout)
    with pytest.raises(claude_cli.ClaudeCliError, match="timed out"):
        claude_cli.run_claude("u", model="m")


# ---- scorer through the CLI ------------------------------------------------------

CFG = {
    "models": {"scorer": "claude-haiku-4-5"},
    "scoring": {"batch_size": 2, "max_retries": 1, "backoff_seconds": 0},
    "expertise": {"bonus_topics": ["CAR-T"]},
}


def _entry(i):
    return dict(
        index=i,
        novelty=5,
        clinical_significance=6,
        audience_interest=7,
        expertise_fit=8,
        timeliness=9,
        evidence_level="phase2",
        hype_risk=4,
        rationale="r",
        suggested_angle="a",
    )


def test_scorer_headless_uses_cli_and_wraps_reply(monkeypatch):
    calls = []

    def fake_run(user, *, system, model, cfg, effort=None):  # no tools for the scorer
        calls.append((user, system, model))
        return "```json\n" + json.dumps({"scores": [_entry(0)]}) + "\n```"

    monkeypatch.setattr(claude_cli, "run_claude", fake_run)
    scorer = Scorer(CFG)
    resp = scorer.create_with_retry("USER")
    assert isinstance(resp, HeadlessResponse)
    assert Scorer.extract_scores(resp) == [_entry(0)]
    assert json.loads(Scorer.raw_json(resp))["backend"] == "claude_code"
    user, system, model = calls[0]
    assert user == "USER" and model == "claude-haiku-4-5"
    assert rubric.TOOL_NAME in system and '"scores"' in system  # schema inlined for the CLI


def test_scorer_headless_bad_json_is_scoring_error_not_retried(monkeypatch):
    n = {"calls": 0}

    def fake_run(user, **kw):
        n["calls"] += 1
        return "Sorry, here are my thoughts instead."

    monkeypatch.setattr(claude_cli, "run_claude", fake_run)
    with pytest.raises(ScoringError, match="not JSON"):
        Scorer(CFG).create_with_retry("USER")
    assert n["calls"] == 1

    monkeypatch.setattr(claude_cli, "run_claude", lambda *a, **k: json.dumps({"foo": 1}))
    with pytest.raises(ScoringError, match="scores"):
        Scorer(CFG).create_with_retry("USER")


def test_scorer_headless_cli_error_is_retried(monkeypatch):
    n = {"calls": 0}

    def flaky(user, **kw):
        n["calls"] += 1
        if n["calls"] == 1:
            raise claude_cli.ClaudeCliError("exited 1: rate limited")
        return json.dumps({"scores": [_entry(0)]})

    monkeypatch.setattr(claude_cli, "run_claude", flaky)
    resp = Scorer(CFG).create_with_retry("USER")
    assert n["calls"] == 2 and Scorer.extract_scores(resp)[0]["index"] == 0


def test_scorer_unavailable_cli_is_not_retried_and_stops_the_run(monkeypatch, db):
    n = {"calls": 0}

    def missing(user, **kw):
        n["calls"] += 1
        raise claude_cli.ClaudeCliUnavailable("could not start 'claude'")

    monkeypatch.setattr(claude_cli, "run_claude", missing)
    scorer = Scorer({**CFG, "scoring": {"batch_size": 1, "max_retries": 5, "backoff_seconds": 0}})
    with pytest.raises(claude_cli.ClaudeCliUnavailable):
        scorer.create_with_retry("USER")
    assert n["calls"] == 1  # no backoff loop for an unfixable error

    # Several unscored batches: the run stops after the first, not one failure per batch.
    from tests.test_scorer import _seed

    _seed(db, 3)
    n["calls"] = 0
    assert scorer.score_unscored(db) == []
    assert n["calls"] == 1


def test_scorer_ignores_a_leftover_api_backend(monkeypatch):
    """An old config.yaml or .env may still say `backend: api`; the call is a CLI run anyway."""
    monkeypatch.setitem(sys.modules, "anthropic", None)  # `import anthropic` now fails
    monkeypatch.setenv("LLM_BACKEND", "api")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    calls = []
    monkeypatch.setattr(
        claude_cli,
        "run_claude",
        lambda user, **kw: calls.append(user) or json.dumps({"scores": [_entry(0)]}),
    )
    cfg = {**CFG, "models": {"scorer": "m", "backend": "api"}}
    resp = Scorer(cfg).create_message("USER")
    assert isinstance(resp, HeadlessResponse) and calls == ["USER"]
    assert Scorer.extract_scores(resp) == [_entry(0)]


# ---- drafter through the CLI ------------------------------------------------------


def test_drafter_call_runs_the_cli_with_the_root_config(monkeypatch):
    monkeypatch.setattr(drafter, "_root_config", lambda: {"claude_code": {"binary": "cc"}})
    seen = {}

    def fake_run(user, *, system, model, cfg, effort=None):  # no tools for the drafter
        seen.update(user=user, system=system, model=model, cfg=cfg, effort=effort)
        return '{"ok": true}'

    monkeypatch.setattr(claude_cli, "run_claude", fake_run)
    assert drafter.call_anthropic("SYS", "USER", "claude-sonnet-5") == '{"ok": true}'
    assert seen == {
        "user": "USER",
        "system": "SYS",
        "model": "claude-sonnet-5",
        "cfg": {"claude_code": {"binary": "cc"}},
        "effort": None,  # no drafter_effort: the model's default
    }


def test_drafter_call_needs_no_api_key_and_ignores_a_leftover_backend(monkeypatch):
    monkeypatch.setitem(sys.modules, "anthropic", None)  # `import anthropic` now fails
    monkeypatch.setenv("LLM_BACKEND", "api")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(drafter, "_root_config", lambda: {"models": {"backend": "api"}})
    calls = []
    monkeypatch.setattr(
        claude_cli, "run_claude", lambda user, **kw: calls.append(user) or "from the CLI"
    )
    assert drafter.call_anthropic("SYS", "USER", "m") == "from the CLI"
    assert calls == ["USER"]


def test_run_with_timeout_kills_the_child_and_raises():
    """A real subprocess: the timeout must end the call (and the process) instead of
    blocking in communicate(), which is what happened on Windows through claude.cmd."""
    argv = [sys.executable, "-c", "import time; time.sleep(30)"]
    with pytest.raises(subprocess.TimeoutExpired):
        claude_cli._run_with_timeout(argv, "", timeout=0.5)


def test_run_with_timeout_runs_from_temp_dir_and_returns_output():
    argv = [sys.executable, "-c", "import os, sys; print(sys.stdin.read() + os.getcwd())"]
    proc = claude_cli._run_with_timeout(argv, "in:", timeout=30)
    assert proc.returncode == 0
    assert proc.stdout.startswith("in:")
    assert os.path.realpath(proc.stdout.strip()[3:]) == os.path.realpath(
        claude_cli.tempfile.gettempdir()
    )


def test_no_window_kwargs_only_on_windows(monkeypatch):
    monkeypatch.setattr(claude_cli.sys, "platform", "linux")
    assert claude_cli.no_window_kwargs() == {}
    monkeypatch.setattr(claude_cli.sys, "platform", "win32")
    monkeypatch.setattr(claude_cli.subprocess, "CREATE_NO_WINDOW", 0x08000000, raising=False)
    assert claude_cli.no_window_kwargs() == {"creationflags": 0x08000000}


def test_the_cli_and_taskkill_are_launched_without_a_console_window(monkeypatch):
    seen = {}

    class FakeProc:
        pid = 1
        returncode = 0

        def communicate(self, *a, **k):
            return "", ""

    def fake_popen(argv, **kwargs):
        seen["popen"] = kwargs
        return FakeProc()

    def fake_run(argv, **kwargs):
        seen["run"] = kwargs

    monkeypatch.setattr(claude_cli, "no_window_kwargs", lambda: {"creationflags": 7})
    monkeypatch.setattr(claude_cli.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(claude_cli.subprocess, "run", fake_run)
    monkeypatch.setattr(claude_cli.sys, "platform", "win32")
    claude_cli._run_with_timeout(["claude"], "", timeout=5)
    assert seen["popen"]["creationflags"] == 7
    claude_cli._kill_tree(FakeProc())
    assert seen["run"]["creationflags"] == 7


def test_safeguard_verdict_is_a_refusal_not_a_plain_error(monkeypatch):
    monkeypatch.setattr(claude_cli.shutil, "which", lambda b: b)
    envelope = json.dumps(
        {
            "is_error": True,
            "terminal_reason": "api_error",
            "result": "API Error: Opus 5's safeguards flagged this message "
            "(https://www.anthropic.com/legal/aup). Claude Code can't respond.",
        }
    )
    monkeypatch.setattr(
        claude_cli,
        "_run_with_timeout",
        lambda *a, **k: SimpleNamespace(returncode=1, stdout=envelope, stderr=""),
    )
    with pytest.raises(claude_cli.ClaudeCliRefused, match="safeguards flagged"):
        claude_cli.run_claude("u", model="m")
    # exit 0 with an error envelope carrying the same verdict
    monkeypatch.setattr(
        claude_cli,
        "_run_with_timeout",
        lambda *a, **k: SimpleNamespace(returncode=0, stdout=envelope, stderr=""),
    )
    with pytest.raises(claude_cli.ClaudeCliRefused):
        claude_cli.run_claude("u", model="m")
    assert issubclass(claude_cli.ClaudeCliRefused, claude_cli.ClaudeCliError)


def test_scorer_refusal_is_not_retried_and_splits_the_batch(monkeypatch, db):
    from tests.test_scorer import _seed

    calls: list[int] = []

    def refuse_big_prompts(user, **kw):
        n = user.count("### Item ")
        calls.append(n)
        if n > 1:
            raise claude_cli.ClaudeCliRefused("CLI exited 1: safeguards flagged this message")
        return json.dumps({"scores": [_entry(0)]})

    monkeypatch.setattr(claude_cli, "run_claude", refuse_big_prompts)
    monkeypatch.setattr("score.scorer.time.sleep", lambda s: None)
    _seed(db, 3)
    scorer = Scorer({**CFG, "scoring": {"batch_size": 3, "max_retries": 5, "backoff_seconds": 0}})
    scores = scorer.score_unscored(db)
    assert len(scores) == 3
    assert calls == [3, 1, 2, 1, 1]  # no backoff retries, one split per refusal


def test_scorer_and_drafter_pass_effort_to_cli(monkeypatch):
    seen = []
    monkeypatch.setattr(
        claude_cli,
        "run_claude",
        lambda user, **kw: seen.append(kw.get("effort")) or json.dumps({"scores": []}),
    )
    Scorer({**CFG, "models": {**CFG["models"], "scorer_effort": "low"}}).create_message("U")
    monkeypatch.setattr(drafter, "_root_config", lambda: {"models": {"drafter_effort": "low"}})
    drafter.call_anthropic("S", "U", "m")
    assert seen == ["low", "low"]


def test_empty_result_names_the_stop_reason():
    import json

    import pytest

    from claude_cli import ClaudeCliError, parse_envelope

    out = json.dumps(
        {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "result": "",
            "stop_reason": "max_tokens",
            "num_turns": 1,
            "usage": {"output_tokens": 4096},
        }
    )
    with pytest.raises(ClaudeCliError, match="stop_reason=max_tokens.*output_tokens=4096"):
        parse_envelope(out)


def _transcript(first_turn):
    import json

    return json.dumps(
        [
            {"type": "system", "subtype": "init"},
            {"type": "assistant", "message": {"content": first_turn}},
            {"type": "user", "message": {"content": [{"type": "tool_result", "is_error": True}]}},
            {"type": "assistant", "message": {"content": [{"type": "text", "text": ""}]}},
            {
                "type": "result",
                "subtype": "success",
                "is_error": False,
                "result": "",
                "stop_reason": "end_turn",
                "num_turns": 2,
            },
        ]
    )


def test_empty_result_recovers_text_from_an_earlier_turn():
    from claude_cli import parse_envelope

    out = _transcript(
        [{"type": "text", "text": '{"scores": []}'}, {"type": "tool_use", "name": "x", "input": {}}]
    )
    assert parse_envelope(out) == '{"scores": []}'


def test_empty_result_recovers_a_failed_tool_call_input():
    import json

    from claude_cli import parse_envelope

    out = _transcript([{"type": "tool_use", "name": "score", "input": {"scores": [1]}}])
    assert json.loads(parse_envelope(out)) == {"scores": [1]}
