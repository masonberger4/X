import json
import logging
from types import SimpleNamespace

import pytest

import claude_cli
from filter.dedup import assign_cluster
from filter.prefilter import run_prefilter
from ingest.base import Item
from score import rubric
from score.scorer import HeadlessToolUse, Scorer, ScoringError, entry_problem

CFG = {
    "models": {"scorer": "test-model-from-config"},
    "scoring": {
        "batch_size": 2,
        "max_retries": 2,
        "backoff_seconds": 0,
        "abstract_max_chars": 100,
    },
    "expertise": {"bonus_topics": ["CAR-T cell therapy", "CRISPR"]},
    "prefilter": {"require_abstract": False, "allow_keywords": [], "daily_cap": 0},
}


def _entry(i, **over):
    d = dict(
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
    d.update(over)
    return d


def _reply(user):
    """The CLI's reply to a batch prompt: the scoring tool's input as bare JSON, one entry
    per item in the prompt."""
    return json.dumps({"scores": [_entry(i) for i in range(user.count("### Item "))]})


def _seed(db, n):
    for i in range(n):
        assign_cluster(
            db,
            Item.build(
                source="s",
                url=f"https://x/{i}",
                title=f"Cancer story number {i} about {['lung', 'breast', 'colon'][i]} tumors",
                abstract="A" * 300,
            ),
        )
    run_prefilter(db, CFG["prefilter"])


def _unscored(db):
    return db.unscored_clusters(CFG["models"]["scorer"], rubric.PROMPT_VERSION)


def test_rubric_total_and_prompt():
    assert rubric.compute_total(_entry(0)) == 35 - 2
    assert rubric.compute_total(_entry(0, hype_risk=10, novelty=0)) == 30 - 5
    sp = rubric.build_system_prompt(CFG["expertise"])
    assert "CAR-T cell therapy" in sp and "CRISPR" in sp and rubric.TOOL_NAME in sp
    assert rubric.TOOL["input_schema"]["properties"]["scores"]["items"]["required"][0] == "index"
    msg = rubric.build_user_message([{"source": "s", "title": "T", "abstract": "A"}])
    assert "### Item 0" in msg and "title: T" in msg


def test_scores_batches_and_stores_metadata(db, monkeypatch):
    _seed(db, 3)
    sent, replies = [], []

    def fake_run(user, **kw):
        sent.append(user)
        replies.append(_reply(user))
        return replies[-1]

    monkeypatch.setattr(claude_cli, "run_claude", fake_run)
    scorer = Scorer(CFG)
    scores = scorer.score_unscored(db)
    assert len(scores) == 3 and len(sent) == 2  # batch_size 2 -> 2 CLI calls
    s = db.latest_score(1)
    assert s.model == "test-model-from-config"
    assert s.prompt_version == rubric.PROMPT_VERSION
    assert s.total == 33 and s.evidence_level == "phase2"
    raw = json.loads(s.raw_response)  # the verbatim CLI reply of cluster 1's batch
    assert raw["text"] == replies[0] and raw["model"] == "test-model-from-config"
    assert "A" * 100 in sent[0] and "A" * 101 not in sent[0]  # abstract truncated
    assert db.unscored_clusters(scorer.model, rubric.PROMPT_VERSION) == []


def test_cli_request_uses_config_model_effort_and_inlined_schema(db, monkeypatch):
    _seed(db, 1)
    calls = []

    def fake_run(user, **kw):
        calls.append(dict(kw, user=user))
        return "Here are the scores.\n" + _reply(user) + "\nDone."  # stray prose is tolerated

    monkeypatch.setattr(claude_cli, "run_claude", fake_run)
    cfg = dict(CFG, claude_code={"binary": "cc", "timeout_seconds": 9})
    assert len(Scorer(cfg).score_unscored(db)) == 1
    (call,) = calls
    assert call["model"] == "test-model-from-config"
    assert call["effort"] is None  # no scorer_effort: the model's default
    assert claude_cli.cli_settings(call["cfg"])["binary"] == "cc"  # claude_code: reaches the CLI
    assert not call.get("tools")  # the scorer's answer is its JSON reply, not a tool call
    system = call["system"]
    assert system.startswith(rubric.build_system_prompt(cfg["expertise"]))
    assert "ONLY a JSON object" in system and f"`{rubric.TOOL_NAME}` tool" in system
    assert json.dumps(rubric.TOOL["input_schema"]) in system  # the schema travels in the prompt
    assert "### Item 0" in call["user"] and "Cancer story number 0" in call["user"]
    assert db.latest_score(1).total == 33  # the JSON inside the prose was parsed and stored

    calls.clear()
    Scorer(dict(cfg, models=dict(cfg["models"], scorer_effort=" High "))).create_message("U")
    assert calls[0]["effort"] == "high"


def test_unusable_reply_is_a_scoring_error_and_the_batch_is_skipped(db, monkeypatch, caplog):
    monkeypatch.setattr(claude_cli, "run_claude", lambda user, **kw: "I cannot score these.")
    with pytest.raises(ScoringError, match="CLI reply is not JSON"):
        Scorer(CFG).create_message("U")
    monkeypatch.setattr(claude_cli, "run_claude", lambda user, **kw: json.dumps({"n": 1}))
    with pytest.raises(ScoringError, match="lacks a 'scores' list"):
        Scorer(CFG).create_message("U")

    _seed(db, 3)
    calls = []

    def first_batch_prose(user, **kw):
        calls.append(user.count("### Item "))
        return "I cannot score these." if len(calls) == 1 else _reply(user)

    monkeypatch.setattr(claude_cli, "run_claude", first_batch_prose)
    with caplog.at_level(logging.ERROR, logger="score.scorer"):
        scores = Scorer(CFG).score_unscored(db)
    assert calls == [2, 1]  # the bad batch is not retried; the next batch still runs
    assert [s.cluster_id for s in scores] == [3]
    assert [c.id for c in _unscored(db)] == [1, 2]  # left for the next run
    assert "CLI reply is not JSON" in caplog.text


def test_a_bad_entry_costs_its_own_cluster_not_the_batch(db, monkeypatch, caplog):
    # The CLI has no strict tool schema: an entry can come back without a field, with a
    # score out of range or a level the rubric does not know. Only that cluster is skipped
    # (and scored again next run); the rest of the batch is stored and the run goes on.
    _seed(db, 3)
    reply = {
        "scores": [
            _entry(0, novelty="7"),  # a number written as text still counts
            {k: v for k, v in _entry(1).items() if k != "clinical_significance"},
            "not an entry",
            {"index": "two"},
        ]
    }

    def fake_run(user, **kw):
        n = user.count("### Item ")
        if n == 2:
            return json.dumps(reply)
        return json.dumps({"scores": [_entry(0, evidence_level="anecdote")]})

    monkeypatch.setattr(claude_cli, "run_claude", fake_run)
    with caplog.at_level(logging.WARNING, logger="score.scorer"):
        scores = Scorer(CFG).score_unscored(db)
    assert [(s.cluster_id, s.novelty) for s in scores] == [(1, 7)]
    assert [c.id for c in _unscored(db)] == [2, 3]
    assert "cluster 2: unusable model output (clinical_significance is not a whole number" in (
        caplog.text
    )
    assert "cluster 3: unusable model output (evidence_level 'anecdote'" in caplog.text
    assert "without a usable index" in caplog.text


@pytest.mark.parametrize(
    ("entry", "problem"),
    [
        (_entry(0), ""),
        (_entry(0, novelty=7.0, hype_risk="3"), ""),
        (_entry(0, novelty=11), "novelty is 11, outside 0-10"),
        (_entry(0, timeliness=-1), "timeliness is -1, outside 0-10"),
        (_entry(0, expertise_fit=7.5), "expertise_fit is not a whole number (7.5)"),
        (_entry(0, audience_interest=True), "audience_interest is not a whole number (True)"),
        (_entry(0, hype_risk=None), "hype_risk is not a whole number (None)"),
        (_entry(0, evidence_level="Phase 2"), "evidence_level 'Phase 2' is not a known level"),
        (_entry(0, rationale=None), "rationale is missing"),
        (["novelty", 5], "not an object"),
    ],
)
def test_entry_problem(entry, problem):
    assert entry_problem(entry) == problem


def test_retry_then_success(db, monkeypatch):
    _seed(db, 1)
    attempts = {"n": 0}

    def flaky(user, **kw):
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise claude_cli.ClaudeCliError("CLI exited 1: rate limited")
        return _reply(user)

    sleeps = []
    monkeypatch.setattr(claude_cli, "run_claude", flaky)
    monkeypatch.setattr("score.scorer.time.sleep", sleeps.append)
    cfg = dict(CFG, scoring=dict(CFG["scoring"], backoff_seconds=1))
    assert len(Scorer(cfg).score_unscored(db)) == 1
    assert attempts["n"] == 3
    assert sleeps == [1, 2]  # exponential backoff between attempts


def test_retry_exhausted_logs_and_continues(db, monkeypatch, caplog):
    _seed(db, 3)
    calls = []

    def first_batch_fails(user, **kw):
        calls.append(user.count("### Item "))
        if len(calls) <= 3:
            raise claude_cli.ClaudeCliError("CLI exited 1: usage limit reached")
        return _reply(user)

    monkeypatch.setattr(claude_cli, "run_claude", first_batch_fails)
    monkeypatch.setattr("score.scorer.time.sleep", lambda s: None)
    with caplog.at_level(logging.WARNING, logger="score.scorer"):
        scores = Scorer(CFG).score_unscored(db)
    assert calls == [2, 2, 2, 1]  # max_retries 2: three tries, then on to the next batch
    assert [s.cluster_id for s in scores] == [3]
    assert [c.id for c in _unscored(db)] == [1, 2]
    assert "usage limit reached" in caplog.text  # the CLI's reason reaches the log


def test_missing_tool_block_and_refusal():
    with pytest.raises(ScoringError, match="no tool_use block"):
        Scorer.extract_scores(SimpleNamespace(stop_reason="end_turn", content=[]))
    block = HeadlessToolUse(rubric.TOOL_NAME, {"scores": []})
    with pytest.raises(ScoringError, match="refused"):  # checked before the tool block
        Scorer.extract_scores(SimpleNamespace(stop_reason="refusal", content=[block]))
