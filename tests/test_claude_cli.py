"""Headless backend (claude_cli) and its use by the scorer and drafter. No subprocess is
ever spawned: subprocess.run / run_claude are replaced."""

from __future__ import annotations

import json
import subprocess
from types import SimpleNamespace

import pytest

import claude_cli
from draft import drafter
from score import rubric
from score.scorer import HeadlessResponse, Scorer, ScoringError

# ---- backend selection ---------------------------------------------------------


def test_backend_defaults_to_api(monkeypatch):
    monkeypatch.delenv("LLM_BACKEND", raising=False)
    assert claude_cli.llm_backend({}) == "api"
    assert claude_cli.llm_backend({"models": {"scorer": "x"}}) == "api"
    assert claude_cli.llm_backend({"models": {"backend": "claude_code"}}) == "claude_code"


def test_backend_env_overrides_config_and_rejects_unknown(monkeypatch):
    monkeypatch.setenv("LLM_BACKEND", "claude_code")
    assert claude_cli.llm_backend({"models": {"backend": "api"}}) == "claude_code"
    monkeypatch.setenv("LLM_BACKEND", "bogus")
    with pytest.raises(ValueError):
        claude_cli.llm_backend({})


def test_shipped_config_defaults_to_api(monkeypatch):
    monkeypatch.delenv("LLM_BACKEND", raising=False)
    import config

    cfg = config.load_config()
    assert claude_cli.llm_backend(cfg) == "api"
    assert claude_cli.cli_settings(cfg)["binary"] == "claude"


# ---- argv and envelope ----------------------------------------------------------


def test_build_argv_is_print_mode_without_tools():
    argv = claude_cli.build_argv(
        "claude-haiku-4-5", "SYS", claude_cli.cli_settings({"claude_code": {"extra_args": ["-x"]}})
    )
    assert argv[:2] == ["claude", "-p"]
    assert argv[argv.index("--output-format") + 1] == "json"
    assert argv[argv.index("--tools") + 1] == ""
    assert argv[argv.index("--model") + 1] == "claude-haiku-4-5"
    assert argv[argv.index("--system-prompt") + 1] == "SYS"
    assert "--no-session-persistence" in argv and "--bare" in argv
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

    def fake_run(argv, **kw):
        seen["argv"], seen["kw"] = argv, kw
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps({"subtype": "success", "result": "answer"}),
            stderr="",
        )

    monkeypatch.setattr(claude_cli.shutil, "which", lambda b: "/usr/bin/" + b)
    monkeypatch.setattr(claude_cli.subprocess, "run", fake_run)
    out = claude_cli.run_claude(
        "USER", system="SYS", model="m", cfg={"claude_code": {"timeout_seconds": 7}}
    )
    assert out == "answer"
    assert seen["kw"]["input"] == "USER"
    assert seen["kw"]["timeout"] == 7.0
    assert "USER" not in seen["argv"]


def test_run_claude_failures_become_cli_errors(monkeypatch):
    monkeypatch.setattr(claude_cli.shutil, "which", lambda b: None)
    with pytest.raises(claude_cli.ClaudeCliError, match="not found"):
        claude_cli.run_claude("u", model="m")

    monkeypatch.setattr(claude_cli.shutil, "which", lambda b: b)
    monkeypatch.setattr(
        claude_cli.subprocess,
        "run",
        lambda *a, **k: SimpleNamespace(returncode=1, stdout="", stderr="Not logged in"),
    )
    with pytest.raises(claude_cli.ClaudeCliError, match="exited 1: Not logged in"):
        claude_cli.run_claude("u", model="m")

    def timeout(*a, **k):
        raise subprocess.TimeoutExpired(cmd="claude", timeout=1)

    monkeypatch.setattr(claude_cli.subprocess, "run", timeout)
    with pytest.raises(claude_cli.ClaudeCliError, match="timed out"):
        claude_cli.run_claude("u", model="m")


# ---- scorer on the headless backend ----------------------------------------------

CFG = {
    "models": {"scorer": "claude-haiku-4-5", "backend": "claude_code"},
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
    monkeypatch.delenv("LLM_BACKEND", raising=False)
    calls = []

    def fake_run(user, *, system, model, cfg):
        calls.append((user, system, model))
        return "```json\n" + json.dumps({"scores": [_entry(0)]}) + "\n```"

    monkeypatch.setattr(claude_cli, "run_claude", fake_run)
    scorer = Scorer(CFG, client=object())  # client must never be touched
    resp = scorer.create_with_retry("USER")
    assert isinstance(resp, HeadlessResponse)
    assert Scorer.extract_scores(resp) == [_entry(0)]
    assert json.loads(Scorer.raw_json(resp))["backend"] == "claude_code"
    user, system, model = calls[0]
    assert user == "USER" and model == "claude-haiku-4-5"
    assert rubric.TOOL_NAME in system and '"scores"' in system  # schema inlined for the CLI


def test_scorer_headless_bad_json_is_scoring_error_not_retried(monkeypatch):
    monkeypatch.delenv("LLM_BACKEND", raising=False)
    n = {"calls": 0}

    def fake_run(user, **kw):
        n["calls"] += 1
        return "Sorry, here are my thoughts instead."

    monkeypatch.setattr(claude_cli, "run_claude", fake_run)
    with pytest.raises(ScoringError, match="not JSON"):
        Scorer(CFG, client=object()).create_with_retry("USER")
    assert n["calls"] == 1

    monkeypatch.setattr(claude_cli, "run_claude", lambda *a, **k: json.dumps({"foo": 1}))
    with pytest.raises(ScoringError, match="scores"):
        Scorer(CFG, client=object()).create_with_retry("USER")


def test_scorer_headless_cli_error_is_retried(monkeypatch):
    monkeypatch.delenv("LLM_BACKEND", raising=False)
    n = {"calls": 0}

    def flaky(user, **kw):
        n["calls"] += 1
        if n["calls"] == 1:
            raise claude_cli.ClaudeCliError("exited 1: rate limited")
        return json.dumps({"scores": [_entry(0)]})

    monkeypatch.setattr(claude_cli, "run_claude", flaky)
    resp = Scorer(CFG, client=object()).create_with_retry("USER")
    assert n["calls"] == 2 and Scorer.extract_scores(resp)[0]["index"] == 0


def test_scorer_api_backend_never_spawns_cli(monkeypatch):
    monkeypatch.delenv("LLM_BACKEND", raising=False)
    monkeypatch.setattr(
        claude_cli, "run_claude", lambda *a, **k: pytest.fail("CLI used on api backend")
    )
    cfg = {**CFG, "models": {"scorer": "m", "backend": "api"}}
    created = {}

    class Client:
        class messages:
            @staticmethod
            def create(**kw):
                created.update(kw)
                return "api-response"

    assert Scorer(cfg, client=Client()).create_message("USER") == "api-response"
    assert created["tools"] == [rubric.TOOL]


# ---- drafter on the headless backend --------------------------------------------


def test_drafter_call_routes_to_cli_when_configured(monkeypatch):
    monkeypatch.setenv("LLM_BACKEND", "claude_code")
    monkeypatch.setattr(drafter, "_root_config", lambda: {"claude_code": {"binary": "cc"}})
    seen = {}

    def fake_run(user, *, system, model, cfg):
        seen.update(user=user, system=system, model=model, cfg=cfg)
        return '{"ok": true}'

    monkeypatch.setattr(claude_cli, "run_claude", fake_run)
    assert drafter.call_anthropic("SYS", "USER", "claude-sonnet-5") == '{"ok": true}'
    assert seen == {
        "user": "USER",
        "system": "SYS",
        "model": "claude-sonnet-5",
        "cfg": {"claude_code": {"binary": "cc"}},
    }


def test_drafter_call_api_backend_requires_key_and_skips_cli(monkeypatch):
    monkeypatch.setenv("LLM_BACKEND", "api")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(drafter, "_root_config", lambda: {})
    monkeypatch.setattr(drafter, "load_dotenv", lambda *a, **k: None)
    monkeypatch.setattr(
        claude_cli, "run_claude", lambda *a, **k: pytest.fail("CLI used on api backend")
    )
    with pytest.raises(RuntimeError, match="ANTHROPIC_API_KEY"):
        drafter.call_anthropic("SYS", "USER", "m")
