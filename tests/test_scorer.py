import json
from types import SimpleNamespace

import anthropic
import pytest

from filter.dedup import assign_cluster
from filter.prefilter import run_prefilter
from ingest.base import Item
from score import rubric
from score.scorer import Scorer, ScoringError

CFG = {
    "models": {"scorer": "test-model-from-config"},
    "scoring": {
        "batch_size": 2,
        "max_tokens": 512,
        "max_retries": 2,
        "backoff_seconds": 0,
        "force_tool_choice": True,
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


class FakeResponse:
    def __init__(self, scores, stop_reason="tool_use"):
        self.stop_reason = stop_reason
        self.content = [
            SimpleNamespace(type="text", text="ok"),
            SimpleNamespace(type="tool_use", name=rubric.TOOL_NAME, input={"scores": scores}),
        ]

    def model_dump_json(self):
        return json.dumps(
            {
                "content": [
                    {
                        "type": "tool_use",
                        "input": {
                            "scores": [c.input for c in self.content if c.type == "tool_use"][0]
                        },
                    }
                ]
            }
        )


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
    sent = []

    def fake_create(self, content):
        sent.append(content)
        n = content.count("### Item ")
        return FakeResponse([_entry(i) for i in range(n)])

    monkeypatch.setattr(Scorer, "create_message", fake_create)
    scorer = Scorer(CFG, client=object())
    scores = scorer.score_unscored(db)
    assert len(scores) == 3 and len(sent) == 2  # batch_size 2 -> 2 calls
    s = db.latest_score(1)
    assert s.model == "test-model-from-config"
    assert s.prompt_version == rubric.PROMPT_VERSION
    assert s.total == 33 and s.evidence_level == "phase2"
    assert json.loads(s.raw_response)["content"][0]["type"] == "tool_use"
    assert "A" * 100 in sent[0] and "A" * 101 not in sent[0]  # abstract truncated
    assert db.unscored_clusters(scorer.model, rubric.PROMPT_VERSION) == []


def test_request_shape_uses_config_model_and_tool(db):
    _seed(db, 1)
    captured = {}

    class FakeMessages:
        def create(self, **kw):
            captured.update(kw)
            return FakeResponse([_entry(0)])

    client = SimpleNamespace(messages=FakeMessages())
    Scorer(CFG, client=client).score_unscored(db)
    assert captured["model"] == "test-model-from-config"
    assert captured["tools"] == [rubric.TOOL]
    assert captured["tool_choice"] == {"type": "tool", "name": rubric.TOOL_NAME}
    assert captured["max_tokens"] == 512
    cfg2 = dict(CFG, scoring=dict(CFG["scoring"], force_tool_choice=False))
    captured.clear()
    Scorer(cfg2, client=client).score_batch(db, [db.get_cluster(1)])
    assert "tool_choice" not in captured


def test_retry_then_success(db, monkeypatch):
    _seed(db, 1)
    attempts = {"n": 0}

    def flaky(self, content):
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise anthropic.APIConnectionError(request=None)
        return FakeResponse([_entry(0)])

    monkeypatch.setattr(Scorer, "create_message", flaky)
    monkeypatch.setattr("score.scorer.time.sleep", lambda s: None)
    assert len(Scorer(CFG, client=object()).score_unscored(db)) == 1
    assert attempts["n"] == 3


def test_retry_exhausted_logs_and_continues(db, monkeypatch):
    _seed(db, 1)

    def always_fail(self, content):
        raise anthropic.APIConnectionError(request=None)

    monkeypatch.setattr(Scorer, "create_message", always_fail)
    monkeypatch.setattr("score.scorer.time.sleep", lambda s: None)
    assert Scorer(CFG, client=object()).score_unscored(db) == []
    assert len(db.unscored_clusters(CFG["models"]["scorer"], rubric.PROMPT_VERSION)) == 1


def test_missing_tool_block_and_refusal():
    with pytest.raises(ScoringError):
        Scorer.extract_scores(SimpleNamespace(stop_reason="end_turn", content=[]))
    with pytest.raises(ScoringError):
        Scorer.extract_scores(FakeResponse([], stop_reason="refusal"))
