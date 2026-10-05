"""claude_cli's long agentic sessions (the studio's one door to Claude): the argv of one
stage, the readable lines of its stream-json events, and run_session against a fake CLI.

The fake is a small Python script that records how it was started (argv, working folder,
environment, stdin, the system prompt file) and prints the stream-json lines a test gives
it. claude_cli launches it in place of the CLI through a copy of `subprocess` local to
claude_cli, so the real Claude Code CLI (on PATH on a developer's machine) never starts.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import types
from pathlib import Path

import pytest

import claude_cli

SID = "0b6c1d2e-3f40-4a5b-8c6d-7e8f9a0b1c2d"
FAKE_BINARY = "fake-claude"  # what resolve_binary returns; the launcher swaps in the script

FAKE_CLI = r"""
import json, os, sys, time

with open(os.environ["FAKE_CLAUDE_SPEC"], encoding="utf-8") as fh:
    spec = json.load(fh)
record_path = os.environ["FAKE_CLAUDE_RECORD"]
args = sys.argv[1:]
record = {
    "argv0": sys.argv[0],
    "argv": args,
    "cwd": os.getcwd(),
    "env": {
        k: os.environ.get(k)
        for k in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "STUDIO_TEST_KEEP")
    },
}
if "--append-system-prompt-file" in args:
    path = args[args.index("--append-system-prompt-file") + 1]
    record["system_file"] = path
    with open(path, encoding="utf-8") as fh:
        record["system"] = fh.read()
record["stdin"] = sys.stdin.buffer.read().decode("utf-8")


def save():
    with open(record_path, "w", encoding="utf-8") as fh:
        json.dump(record, fh)


def emit(lines):
    for line in lines:
        data = bytes.fromhex(line["hex"]) if isinstance(line, dict) else line.encode("utf-8")
        sys.stdout.buffer.write(data + b"\n")
        sys.stdout.buffer.flush()


save()
emit(spec["lines"])
if spec.get("wait_for"):
    deadline = time.time() + 15
    while not os.path.exists(spec["wait_for"]) and time.time() < deadline:
        time.sleep(0.02)
    record["gate_seen"] = os.path.exists(spec["wait_for"])
    save()
    emit(spec["after"])
if spec["stderr"]:
    sys.stderr.write(spec["stderr"])
    sys.stderr.flush()
time.sleep(spec["sleep"])
sys.exit(spec["exit"])
"""


# ---- stream-json events, as the CLI prints them -------------------------------------


def init_event(session_id=SID):
    return {
        "type": "system",
        "subtype": "init",
        "session_id": session_id,
        "model": "claude-opus-5-5",
        "tools": ["Read", "WebSearch"],
    }


def assistant_event(*blocks):
    return {
        "type": "assistant",
        "session_id": SID,
        "message": {"role": "assistant", "content": list(blocks)},
    }


def text(t):
    return {"type": "text", "text": t}


def tool(name, args):
    return {"type": "tool_use", "id": "toolu_1", "name": name, "input": args}


def result_event(**over):
    event = {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "result": "Wrote factbase.md and research.json.",
        "session_id": SID,
        "num_turns": 7,
        "total_cost_usd": 1.25,
        "duration_ms": 4000,
        "terminal_reason": "completed",
    }
    event.update(over)
    return event


def _wire(line):
    """One line of the fake's output: an event as compact JSON (the CLI's form), text as
    is, bytes as hex (so a test can send bytes that are not UTF-8)."""
    if isinstance(line, dict):
        return json.dumps(line, separators=(",", ":"))
    if isinstance(line, bytes):
        return {"hex": line.hex()}
    return line


def _shown(line):
    """The same line as it lands in the transcript."""
    if isinstance(line, dict):
        return json.dumps(line, separators=(",", ":"))
    if isinstance(line, bytes):
        return line.decode("utf-8", errors="replace")
    return line


class FakeCli:
    """The stand-in CLI: a script, what it should print, and what it recorded."""

    def __init__(self, root: Path, monkeypatch):
        root.mkdir()
        self.script = root / "fake_claude.py"
        self.script.write_text(f"#!{sys.executable}\n{FAKE_CLI}", encoding="utf-8")
        self.spec_path = root / "spec.json"
        self.record_path = root / "record.json"
        self.launches: list[list[str]] = []
        self.settings: list[dict] = []
        monkeypatch.setenv("FAKE_CLAUDE_SPEC", str(self.spec_path))
        monkeypatch.setenv("FAKE_CLAUDE_RECORD", str(self.record_path))
        self.program(lines=[init_event(), result_event()])

    def program(self, *, lines=(), after=(), wait_for=None, exit=0, stderr="", sleep=0.0):
        spec = {
            "lines": [_wire(x) for x in lines],
            "after": [_wire(x) for x in after],
            "wait_for": str(wait_for) if wait_for else "",
            "exit": exit,
            "stderr": stderr,
            "sleep": sleep,
        }
        self.spec_path.write_text(json.dumps(spec), encoding="utf-8")

    def install(self, monkeypatch):
        """claude_cli starts the script whenever it would start FAKE_BINARY. Only
        claude_cli's own view of `subprocess` is replaced, and anything else it launches
        (taskkill on Windows) goes to the real Popen."""
        real_popen = subprocess.Popen
        script, launches = self.script, self.launches

        def popen(argv, *args, **kwargs):
            if argv and argv[0] == FAKE_BINARY:
                launches.append(list(argv))
                argv = [sys.executable, str(script), *argv[1:]]
            return real_popen(argv, *args, **kwargs)

        local = types.SimpleNamespace(
            **{k: getattr(subprocess, k) for k in dir(subprocess) if not k.startswith("__")}
        )
        local.Popen = popen
        monkeypatch.setattr(claude_cli, "subprocess", local)

        def resolve(settings):
            self.settings.append(dict(settings))
            return FAKE_BINARY

        monkeypatch.setattr(claude_cli, "resolve_binary", resolve)

    @property
    def record(self) -> dict:
        return json.loads(self.record_path.read_text(encoding="utf-8"))


@pytest.fixture
def fake(tmp_path, monkeypatch):
    cli = FakeCli(tmp_path / "fakecli", monkeypatch)
    cli.install(monkeypatch)
    return cli


@pytest.fixture
def workspace(tmp_path):
    ws = tmp_path / "piece workspace"  # a space, as a Windows profile path often has
    ws.mkdir()
    return ws


def run(workspace, prompt="Research the topic.", **kw):
    kw.setdefault("model", "claude-opus-5-5")
    kw.setdefault("session_id", SID)
    kw.setdefault("timeout", 60)
    return claude_cli.run_session(prompt, cwd=workspace, **kw)


def opt(argv, name):
    """The value after `name` in an argv."""
    return argv[argv.index(name) + 1]


# ---- session_argv -------------------------------------------------------------------

FLAGS = ["--safe-mode", "--restricted", "--permission-mode", "dontAsk"]


def _argv(**over):
    kw = dict(
        model="claude-opus-5-5",
        session_id=SID,
        resume=False,
        effort="max",
        tools=["Read", "Write", "WebSearch"],
        allowed=["Read", "Write", "WebSearch"],
        add_dirs=["/refs/exemplars"],
        system_file="/tmp/claude-system-x.md",
        max_turns=40,
        flags=FLAGS,
    )
    kw.update(over)
    return claude_cli.session_argv("/usr/local/bin/claude", **kw)


def test_a_new_session_is_created_under_its_own_id_with_the_system_prompt_appended():
    argv = _argv()
    assert argv[:2] == ["/usr/local/bin/claude", "-p"]
    assert opt(argv, "--output-format") == "stream-json" and "--verbose" in argv
    assert opt(argv, "--model") == "claude-opus-5-5"
    assert opt(argv, "--session-id") == SID and "--resume" not in argv
    # appended to Claude Code's own system prompt, from a file, never on the command line
    assert opt(argv, "--append-system-prompt-file") == "/tmp/claude-system-x.md"
    assert "--system-prompt-file" not in argv and "--system-prompt" not in argv
    assert opt(argv, "--tools") == "Read,Write,WebSearch"
    assert opt(argv, "--allowedTools") == "Read,Write,WebSearch"
    assert opt(argv, "--effort") == "max" and opt(argv, "--max-turns") == "40"
    # a session must stay resumable: print mode's one-shot switches are not used
    assert "--no-session-persistence" not in argv and "--bare" not in argv


def test_a_resumed_session_names_the_session_it_continues():
    argv = _argv(resume=True, system_file=None)
    assert opt(argv, "--resume") == SID
    assert "--session-id" not in argv and "--append-system-prompt-file" not in argv
    # with the standing instructions, which a resume carries too
    argv = _argv(resume=True)
    assert opt(argv, "--resume") == SID
    assert opt(argv, "--append-system-prompt-file") == "/tmp/claude-system-x.md"


def test_each_extra_folder_gets_its_own_add_dir_with_one_value():
    argv = _argv(add_dirs=["/refs/a", "/refs/b c", "/refs/d"])
    at = [i for i, a in enumerate(argv) if a == "--add-dir"]
    assert [argv[i + 1] for i in at] == ["/refs/a", "/refs/b c", "/refs/d"]
    # --add-dir takes any number of values: each one is closed by the next option, and
    # the prompt (stdin) never follows one
    assert all(argv[i + 2].startswith("--") for i in at)


def test_unset_switches_are_left_out():
    argv = _argv(effort=None, max_turns=None, system_file=None, allowed=[], add_dirs=[], flags=[])
    for flag in (
        "--effort",
        "--max-turns",
        "--append-system-prompt-file",
        "--allowedTools",
        "--add-dir",
    ):
        assert flag not in argv
    assert "--max-turns" not in _argv(max_turns=0)  # 0 is "no limit" in studio/config.yaml
    assert opt(_argv(max_turns=12.0), "--max-turns") == "12"
    # no tools means none at all, not the CLI's default set
    assert opt(_argv(tools=[]), "--tools") == ""


def test_the_isolation_flags_come_last():
    assert _argv()[-4:] == FLAGS
    assert _argv(flags=["--safe-mode", 7])[-2:] == ["--safe-mode", "7"]


# ---- describe_event -----------------------------------------------------------------


def test_the_init_event_names_the_model_and_the_tools():
    assert claude_cli.describe_event(init_event()) == [
        "session started (claude-opus-5-5); tools: Read, WebSearch"
    ]
    assert claude_cli.describe_event({"type": "system", "subtype": "compact_boundary"}) == []


def test_a_tool_call_shows_its_tool_and_its_key_argument():
    event = assistant_event(
        tool("WebSearch", {"query": "  next-gen CTLA-4\n Fc-enhanced  "}),
        tool("WebFetch", {"url": "https://www.fda.gov/x", "prompt": "summarise the label"}),
        tool("Write", {"file_path": "factbase.md", "content": "# Facts"}),
        tool("Grep", {"pattern": "ORR", "path": "."}),
        tool("Agent", {"description": "cold fact-check", "prompt": "check every number"}),
        tool("TodoWrite", {"todos": []}),
        tool("Odd", "not a dict"),
    )
    assert claude_cli.describe_event(event) == [
        "[WebSearch] next-gen CTLA-4 Fc-enhanced",
        "[WebFetch] https://www.fda.gov/x",
        "[Write] factbase.md",
        "[Grep] ORR",
        "[Agent] cold fact-check",
        "[TodoWrite]",
        "[Odd]",
    ]


def test_a_long_tool_argument_is_clipped_at_160_characters():
    def line(query):
        (out,) = claude_cli.describe_event(assistant_event(tool("WebSearch", {"query": query})))
        return out

    assert line("q" * 160) == "[WebSearch] " + "q" * 160
    assert line("q" * 161) == "[WebSearch] " + "q" * 160 + "..."


def test_the_assistants_text_is_one_line_clipped_at_240_characters():
    def lines(*blocks):
        return claude_cli.describe_event(assistant_event(*blocks))

    assert lines(text("Found   the\n\nprimary source.")) == ["Found the primary source."]
    assert lines(text("w" * 240)) == ["w" * 240]
    assert lines(text("w" * 241)) == ["w" * 240 + "..."]
    # tool call and text in one message, in order; blank text, thinking and junk say nothing
    assert lines(
        {"type": "thinking", "thinking": "hmm"},
        text("Checking the label."),
        "junk",
        text("   "),
        tool("Read", {"file_path": "factbase.md"}),
    ) == ["Checking the label.", "[Read] factbase.md"]


def test_events_without_anything_to_say_give_no_lines():
    assert claude_cli.describe_event({"type": "assistant"}) == []
    assert claude_cli.describe_event({"type": "assistant", "message": {"content": "plain"}}) == []
    tool_result = {"type": "tool_result", "tool_use_id": "toolu_1", "content": "page text"}
    assert claude_cli.describe_event({"type": "user", "message": {"content": [tool_result]}}) == []
    assert claude_cli.describe_event({}) == []


@pytest.mark.parametrize(
    "event",
    [
        {"type": "assistant", "message": "not an object"},
        {"type": "assistant", "message": ["a", "list"]},
        {"type": "assistant", "message": {"content": [{"type": "tool_use", "input": None}]}},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": None}]}},
        {"type": "system", "subtype": "init", "tools": None},
        {"type": "result"},
    ],
)
def test_a_malformed_event_never_raises(event):
    """Every line of a long session goes through this; one odd event must cost at most its
    own log line, never the lines after it."""
    assert isinstance(claude_cli.describe_event(event), list)


def test_the_result_event_says_how_the_stage_ended():
    assert claude_cli.describe_event(result_event(num_turns=12)) == [
        "stage ended: success after 12 turns"
    ]
    assert claude_cli.describe_event(result_event(subtype="error_max_turns", num_turns=40)) == [
        "stage ended: error_max_turns after 40 turns"
    ]


# ---- session_started, cli_env, SessionResult ----------------------------------------


def test_session_started_looks_for_the_init_event_of_that_session(tmp_path):
    path = tmp_path / "session.ndjson"
    assert not claude_cli.session_started(path, SID)  # no transcript yet
    path.write_bytes(b"\xff\xfe not text\n" + json.dumps(assistant_event(text("hi"))).encode())
    assert not claude_cli.session_started(path, SID)  # this session never said init
    with open(path, "a", encoding="utf-8") as fh:
        fh.write("\n" + json.dumps(init_event(session_id="an-earlier-session")) + "\n")
    assert not claude_cli.session_started(path, SID)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(init_event()) + "\n")  # json.dumps puts a space after ':'
    assert claude_cli.session_started(path, SID)
    compact = tmp_path / "compact.ndjson"
    compact.write_text(json.dumps(init_event(), separators=(",", ":")) + "\n", encoding="utf-8")
    assert claude_cli.session_started(compact, SID)


def test_the_cli_never_sees_an_api_credential(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-secret")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "token-secret")
    monkeypatch.setenv("STUDIO_TEST_KEEP", "kept")
    env = claude_cli.cli_env()
    assert "ANTHROPIC_API_KEY" not in env and "ANTHROPIC_AUTH_TOKEN" not in env
    assert env["STUDIO_TEST_KEEP"] == "kept" and env["PATH"] == os.environ["PATH"]
    assert os.environ["ANTHROPIC_API_KEY"] == "sk-ant-secret"  # our own is untouched


def test_session_result_detail_says_why_a_stage_stopped():
    def detail(**kw):
        return claude_cli.SessionResult("s", **kw).detail

    assert detail(ok=True, subtype="success", text="all done") == "finished"
    assert (
        detail(ok=False, subtype="success", terminal_reason="api_error", text="API Error:\n 529")
        == "api_error: API Error: 529"
    )
    assert (
        detail(ok=False, subtype="no_result", terminal_reason="CLI exited 1", stderr_tail="boom\n")
        == "CLI exited 1: boom"
    )
    assert detail(ok=False, subtype="error_max_turns") == "error_max_turns"
    assert detail(ok=False) == "error"
    assert detail(ok=False, subtype="x", text="y" * 1000) == "x: " + "y" * 300


# ---- run_session against the fake CLI -----------------------------------------------


def test_every_line_goes_to_the_transcript_and_every_event_to_the_callback(fake, workspace):
    printed = [
        init_event(),
        "Warning: a line that is not JSON",
        "[1, 2]",
        "",
        b"\xff\xfe noise that is not UTF-8",
        assistant_event(tool("WebSearch", {"query": "ipilimumab label"})),
        result_event(),
    ]
    fake.program(lines=printed)
    transcript = workspace / "session.ndjson"
    transcript.write_text('{"type":"earlier stage"}\n', encoding="utf-8")
    seen = []

    def on_event(event):
        line = json.dumps(event, separators=(",", ":"))
        seen.append((event["type"], line in transcript.read_text(encoding="utf-8")))

    result = run(workspace, transcript=transcript, on_event=on_event)
    assert transcript.read_text(encoding="utf-8") == '{"type":"earlier stage"}\n' + "".join(
        _shown(p) + "\n" for p in printed
    )
    # only JSON objects reach the callback, in order, each already in the transcript
    assert seen == [("system", True), ("assistant", True), ("result", True)]
    assert result.ok and result.subtype == "success" and result.detail == "finished"
    assert result.text == "Wrote factbase.md and research.json."
    assert (result.num_turns, result.cost_usd, result.duration_ms) == (7, 1.25, 4000)
    assert result.returncode == 0 and result.terminal_reason == "" and result.session_id == SID


def test_the_prompt_goes_on_stdin_from_the_workspace_without_api_credentials(
    fake, workspace, monkeypatch
):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-never-reaches-the-cli")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "token-never-reaches-the-cli")
    monkeypatch.setenv("STUDIO_TEST_KEEP", "kept")
    prompt = "Research CTLA-4 again: naïve, 2.5×, α-PD-1, 𝛼 — the long brief.\n" * 4000
    assert run(workspace, prompt=prompt).ok
    rec = fake.record
    # ~260 KB, past any argv limit, intact (a text pipe writes \r\n on Windows)
    assert rec["stdin"].replace("\r\n", "\n") == prompt
    assert not any("Research CTLA-4" in a for a in rec["argv"])
    assert Path(rec["cwd"]).resolve() == workspace.resolve()
    assert rec["env"] == {
        "ANTHROPIC_API_KEY": None,
        "ANTHROPIC_AUTH_TOKEN": None,
        "STUDIO_TEST_KEEP": "kept",
    }
    assert os.environ["ANTHROPIC_API_KEY"] == "sk-ant-never-reaches-the-cli"


def test_a_new_session_gets_its_system_prompt_from_a_temp_file_that_is_removed(fake, workspace):
    refs = workspace.parent / "refs"
    result = run(
        workspace,
        system="You are the studio.\nWrite like the exemplars.",
        effort="max",
        tools=["Read", "Write", "Agent"],
        add_dirs=[refs],
        flags=["--safe-mode", "--restricted"],
        max_turns=40,
        cfg={"claude_code": {"binary": "my-claude", "extra_args": ["--debug-file", "x.log"]}},
    )
    assert result.ok
    rec, args = fake.record, fake.record["argv"]
    assert fake.settings[-1]["binary"] == "my-claude"  # resolved from the config's settings
    assert fake.launches[0][0] == FAKE_BINARY  # the resolved path is what is started
    assert opt(args, "--session-id") == SID and "--resume" not in args
    assert rec["system"] == "You are the studio.\nWrite like the exemplars."
    assert "You are the studio." not in " ".join(args)
    assert not Path(rec["system_file"]).exists()  # cleaned up after the stage
    assert opt(args, "--tools") == "Read,Write,Agent"
    assert opt(args, "--allowedTools") == "Read,Write,Agent"  # allowed defaults to the tools
    assert opt(args, "--add-dir") == str(refs)
    assert opt(args, "--effort") == "max" and opt(args, "--max-turns") == "40"
    # the stage's flags, then the config's extra_args, last
    assert args[-4:] == ["--safe-mode", "--restricted", "--debug-file", "x.log"]


def test_a_resumed_session_gets_its_system_prompt_again(fake, workspace, tmp_path, monkeypatch):
    """The CLI reuses its record of the first launch's system prompt only until the
    conversation is compacted; after that it builds the prompt from the current launch's
    flags, so a resume without the file would go on without the standing instructions."""
    tmp = tmp_path / "tmp"
    tmp.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(tmp))
    assert run(workspace, resume=True, system="You are the studio.").ok
    rec = fake.record
    assert opt(rec["argv"], "--resume") == SID and "--session-id" not in rec["argv"]
    assert opt(rec["argv"], "--append-system-prompt-file") == rec["system_file"]
    assert rec["system"] == "You are the studio."
    assert list(tmp.iterdir()) == []  # the file is removed after the stage
    # no standing instructions given: none sent
    assert run(workspace, resume=True).ok
    assert "--append-system-prompt-file" not in fake.record["argv"]


def test_allowed_tools_default_to_the_tools_and_can_be_narrower(fake, workspace):
    run(workspace, tools=["Read", "WebSearch"])
    assert opt(fake.record["argv"], "--allowedTools") == "Read,WebSearch"
    run(workspace, tools=["Read", "WebSearch"], allowed=["Read"])
    assert opt(fake.record["argv"], "--allowedTools") == "Read"
    run(workspace, tools=None)
    args = fake.record["argv"]
    assert opt(args, "--tools") == "" and "--allowedTools" not in args


def test_an_error_result_is_returned_with_its_reason(fake, workspace):
    fake.program(
        lines=[
            init_event(),
            result_event(
                subtype="error_max_turns",
                is_error=True,
                result="",
                num_turns=40,
                terminal_reason="max_turns",
            ),
        ],
        exit=1,
    )
    r = run(workspace)
    assert not r.ok and r.subtype == "error_max_turns" and r.num_turns == 40
    assert r.terminal_reason == "max_turns" and r.returncode == 1 and r.detail == "max_turns"

    # a failed API call: subtype success, is_error and the reason in terminal_reason
    fake.program(
        lines=[
            result_event(
                is_error=True, terminal_reason="api_error", result="API Error: 529 overloaded"
            )
        ],
        exit=1,
    )
    r = run(workspace)
    assert not r.ok and r.subtype == "success"
    assert r.detail == "api_error: API Error: 529 overloaded"

    # a success line from a CLI that then exits non-zero is not a clean finish, and the exit
    # (not "success") is the reason it gives
    fake.program(lines=[result_event()], exit=1)
    r = run(workspace)
    assert not r.ok and r.returncode == 1 and r.subtype == "success"
    assert r.terminal_reason == "CLI exited 1"
    assert r.detail == "CLI exited 1: Wrote factbase.md and research.json."


def test_a_safeguard_verdict_is_reported_as_refused(fake, workspace):
    verdict = (
        "API Error: Claude Opus 5's safeguards flagged this message "
        "(https://www.anthropic.com/legal/aup). Claude Code can't respond."
    )
    fake.program(
        lines=[result_event(is_error=True, terminal_reason="api_error", result=verdict)], exit=1
    )
    r = run(workspace)
    assert not r.ok and r.subtype == "refused" and "safeguards flagged" in r.detail
    # a clean finish that merely quotes the phrase is still a clean finish
    fake.program(lines=[result_event(result="Checked: the safeguards flagged nothing.")])
    r = run(workspace)
    assert r.ok and r.subtype == "success"


def killed_task(task_id="abe56c825a1075442"):
    """The CLI stopping a sub-agent that was still running when the turn ended."""
    return {
        "type": "system",
        "subtype": "task_updated",
        "task_id": task_id,
        "patch": {"status": "killed", "end_time": 1791176184374},
        "session_id": SID,
    }


def subagent_stats(completed=0, killed_by_system=0):
    """A result line's subagent_stats, in the CLI's shape."""
    return {
        "spawned": completed + killed_by_system,
        "completed": completed,
        "failed": 0,
        "killed": {"parent": 0, "user": 0, "system": killed_by_system},
    }


def test_a_stage_whose_sub_agent_was_killed_did_not_finish(fake, workspace):
    """The live run: the cold fact-check was moved to the background, the turn ended, the
    CLI stopped the checker and reported success. The stage did not finish: it says so and
    can be resumed."""
    text = "The cold fact-check is still running in the background. The stage isn't finished."
    fake.program(
        lines=[
            init_event(),
            assistant_event(tool("Agent", {"description": "Cold fact-check"})),
            killed_task(),
            result_event(result=text, subagent_stats=subagent_stats(killed_by_system=1)),
        ]
    )
    seen = []
    r = run(workspace, on_event=seen.append)
    assert not r.ok and r.subtype == claude_cli.SUBAGENT_KILLED
    assert r.terminal_reason == "the CLI stopped 1 sub-agent(s) before they finished"
    assert r.detail == f"the CLI stopped 1 sub-agent(s) before they finished: {text}"
    assert r.returncode == 0 and r.num_turns == 7  # the run itself ended normally
    lines = [line for e in seen for line in claude_cli.describe_event(e)]
    assert "a sub-agent was stopped before it finished (task abe56c825a1075442)" in lines


def test_a_sub_agent_killed_in_an_earlier_turn_still_counts(fake, workspace):
    """The CLI starts another turn after a background sub-agent ends; the last result line
    counts only its own turn, so the task events of the whole run decide."""
    fake.program(
        lines=[
            init_event(),
            killed_task("t1"),
            killed_task("t2"),
            killed_task("t2"),  # the same task said twice is one
            result_event(subagent_stats=subagent_stats()),
        ]
    )
    r = run(workspace)
    assert not r.ok and r.subtype == claude_cli.SUBAGENT_KILLED
    assert r.terminal_reason == "the CLI stopped 2 sub-agent(s) before they finished"


def test_sub_agents_that_all_reported_leave_a_clean_finish(fake, workspace):
    fake.program(lines=[init_event(), result_event(subagent_stats=subagent_stats(completed=4))])
    r = run(workspace)
    assert r.ok and r.subtype == "success" and r.detail == "finished"
    # an older CLI's result line without the stats, or with a number where the counts go
    fake.program(lines=[result_event(subagent_stats={"killed": 0})])
    assert run(workspace).ok
    fake.program(lines=[result_event(subagent_stats={"killed": 2})])
    assert run(workspace).subtype == claude_cli.SUBAGENT_KILLED


def test_a_stage_that_failed_anyway_keeps_its_own_reason(fake, workspace):
    fake.program(
        lines=[
            killed_task(),
            result_event(
                subtype="error_max_turns", is_error=True, terminal_reason="max_turns", result=""
            ),
        ],
        exit=1,
    )
    r = run(workspace)
    assert not r.ok and (r.subtype, r.terminal_reason) == ("error_max_turns", "max_turns")


def test_a_cli_that_exits_without_a_result_says_so(fake, workspace):
    fake.program(
        lines=[init_event(), assistant_event(text("Starting the research."))],
        stderr="Error: Invalid API key · Please run /login\n",
        exit=1,
    )
    transcript = workspace / "session.ndjson"
    r = run(workspace, transcript=transcript)
    assert not r.ok and r.subtype == "no_result" and r.text == ""
    assert r.terminal_reason == "CLI exited 1" and r.returncode == 1
    assert "Invalid API key" in r.stderr_tail
    assert r.detail.startswith("CLI exited 1: Error: Invalid API key")
    assert r.session_id == SID
    # the CLI did start this session, so the next invocation must resume it
    assert claude_cli.session_started(transcript, SID)


def test_a_resume_of_a_session_the_cli_no_longer_has_is_lost(fake, workspace):
    """Claude Code cleans up old sessions (cleanupPeriodDays); a --resume of one prints
    this and exits 1. No later --resume can work, which session_lost tells apart from a
    run that merely stopped."""
    fake.program(stderr=f"No conversation found with session ID: {SID}\n", exit=1)
    r = run(workspace, resume=True)
    assert not r.ok and r.subtype == "no_result" and r.returncode == 1
    assert claude_cli.session_lost(r)
    # said in an error result instead, it is the same verdict
    said = claude_cli.SessionResult(
        session_id=SID,
        ok=False,
        subtype="error_during_execution",
        text=f"No conversation found with session ID: {SID}",
    )
    assert claude_cli.session_lost(said)


@pytest.mark.parametrize(
    "result",
    [
        claude_cli.SessionResult(session_id=SID, ok=False, subtype="killed"),
        claude_cli.SessionResult(
            session_id=SID, ok=False, subtype="no_result", stderr_tail="Error: Invalid API key"
        ),
        claude_cli.SessionResult(session_id=SID, ok=True, subtype="success"),
    ],
)
def test_a_session_that_stopped_for_another_reason_is_not_lost(result):
    assert not claude_cli.session_lost(result)


def test_a_stage_past_its_time_limit_is_killed_and_can_be_resumed(fake, workspace):
    fake.program(lines=[init_event()], sleep=60)
    transcript = workspace / "session.ndjson"
    seen = []
    t0 = time.monotonic()
    r = run(workspace, timeout=2.5, transcript=transcript, on_event=seen.append)
    assert time.monotonic() - t0 < 30, "the watchdog did not end the stage"
    assert not r.ok and r.subtype == "killed" and r.terminal_reason == "timed out"
    assert r.returncode not in (0, None) and r.detail == "timed out"
    assert [e["type"] for e in seen] == ["system"]
    assert claude_cli.session_started(transcript, SID)


def test_a_stage_killed_after_an_earlier_result_line_is_still_killed(fake, workspace):
    """The CLI ends a turn with a result line and starts another when a background
    sub-agent finishes; the time limit can fall in that second turn. The stage stopped
    early (resumable), it did not finish: never 'success'."""
    fake.program(lines=[init_event(), result_event(session_id="forked-session")], sleep=60)
    t0 = time.monotonic()
    r = run(workspace, timeout=2.5)
    assert time.monotonic() - t0 < 30, "the watchdog did not end the stage"
    assert not r.ok and r.subtype == "killed" and r.terminal_reason == "timed out"
    assert r.detail == "timed out" and r.text == ""
    assert r.returncode not in (0, None)
    # what the finished turn reported is kept: its session, turns, cost and time
    assert r.session_id == "forked-session"
    assert (r.num_turns, r.cost_usd, r.duration_ms) == (7, 1.25, 4000)


def test_no_time_limit_lets_a_slow_stage_finish(fake, workspace):
    fake.program(lines=[init_event(), result_event()], sleep=1.0)
    for unlimited in (None, 0):
        r = run(workspace, timeout=unlimited)
        assert r.ok and r.subtype == "success"


def test_events_reach_the_callback_while_the_cli_is_still_running(fake, workspace, tmp_path):
    gate = tmp_path / "gate"
    fake.program(lines=[init_event()], wait_for=gate, after=[result_event()])

    def on_event(event):
        if event["type"] == "system":
            gate.write_text("seen", encoding="utf-8")  # the fake waits for this

    assert run(workspace, on_event=on_event).ok
    assert fake.record["gate_seen"] is True


def test_a_failing_display_hook_never_ends_the_session(fake, workspace):
    transcript = workspace / "session.ndjson"

    def broken(event):
        raise RuntimeError("the page that shows the log broke")

    r = run(workspace, transcript=transcript, on_event=broken)
    assert r.ok
    assert len(transcript.read_text(encoding="utf-8").splitlines()) == 2


def test_the_session_id_the_cli_reports_wins(fake, workspace):
    fake.program(lines=[result_event(session_id="forked-session")])
    assert run(workspace).session_id == "forked-session"
    fake.program(lines=[result_event(session_id=None)])
    assert run(workspace).session_id == SID


def test_a_transcript_that_cannot_be_written_stops_the_launch(fake, workspace, tmp_path):
    with pytest.raises(OSError):
        run(workspace, transcript=tmp_path / "no-such-folder" / "session.ndjson")
    assert fake.launches == []  # no session was left running with nobody reading it


def test_a_cli_that_is_not_installed_is_unavailable(workspace):
    with pytest.raises(claude_cli.ClaudeCliUnavailable, match="not found on PATH"):
        run(workspace, cfg={"claude_code": {"binary": "no-such-claude-cli-for-tests"}})


def test_a_cli_that_cannot_start_is_unavailable_and_leaves_no_temp_file(
    fake, workspace, tmp_path, monkeypatch
):
    tmp = tmp_path / "tmp"
    tmp.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(tmp))
    missing = str(tmp_path / "missing" / "claude")
    monkeypatch.setattr(claude_cli, "resolve_binary", lambda settings: missing)
    with pytest.raises(claude_cli.ClaudeCliUnavailable, match="could not start"):
        run(workspace, system="a system prompt", transcript=workspace / "session.ndjson")
    assert list(tmp.iterdir()) == []


@pytest.mark.skipif(sys.platform == "win32", reason="a #! script is not a program on Windows")
def test_the_configured_binary_is_started_by_its_resolved_path(tmp_path, workspace, monkeypatch):
    cli = FakeCli(tmp_path / "fakecli", monkeypatch)  # not installed: the real resolve_binary
    cli.script.chmod(0o755)
    r = run(workspace, cfg={"claude_code": {"binary": str(cli.script)}})
    assert r.ok
    assert cli.record["argv0"] == str(cli.script) and cli.record["argv"][0] == "-p"
