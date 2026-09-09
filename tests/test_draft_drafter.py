import json

import pytest

from draft.drafter import (
    DraftRejected,
    check_hard_rules,
    draft_item,
    flag_unverified_numbers,
    numbers_in,
    parse_json_response,
    verify_numbers,
)
from draft.schema import validate_output

URL = "https://doi.org/10.1000/xyz123"
ABSTRACT = (
    "In this phase 2 trial of 97 patients, the overall response rate was 88% and median "
    "PFS was 14.6 months. Grade 3 CRS occurred in 4 patients (4.1%)."
)


def good_json(**overrides):
    data = {
        "single_post": f"ORR 88% in 97 patients, single-arm. Sequencing vs bispecifics? {URL}",
        "thread": [
            "Phase 2 CAR-T data in relapsed myeloma: ORR 88%, median PFS 14.6 months.",
            "Single-arm, so no comparator. The sequencing question is open.",
            f"Grade 3 CRS in 4 patients. Source: {URL}",
        ],
        "suggested_visual": "waterfall plot",
        "why_it_matters": "Sequencing vs bispecifics is the real question.",
        "claims_to_verify": [],
    }
    data.update(overrides)
    return data


def fake_call(responses):
    """Return a call() stub that yields each response in turn (exceptions are raised)."""
    it = iter(responses)
    calls = []

    def _call(system, user, model):
        calls.append((system, user, model))
        r = next(it)
        if isinstance(r, Exception):
            raise r
        return r if isinstance(r, str) else json.dumps(r)

    _call.calls = calls
    return _call


def run(call, **kw):
    params = dict(
        title="CAR-T in myeloma", abstract=ABSTRACT, url=URL, source="pubmed", sleep=lambda s: None
    )
    params.update(kw)
    return draft_item(call=call, **params)


# --- happy path ------------------------------------------------------------


def test_draft_item_success_and_prompt_plumbing(monkeypatch):
    monkeypatch.setenv("DRAFT_MODEL", "claude-test-model")
    call = fake_call([good_json()])
    result = run(call, suggested_angle="sequencing")
    assert result.attempts == 1
    assert result.model == "claude-test-model"
    assert result.flagged_numbers == []
    assert result.draft.claims_to_verify == []
    system, user, model = call.calls[0]
    assert "HARD RULES" in system and "sequencing" in user and model == "claude-test-model"


def test_parse_json_tolerates_fences_and_prose():
    assert parse_json_response('```json\n{"a": 1}\n```') == {"a": 1}
    assert parse_json_response('Here you go:\n{"a": 1}\nDone.') == {"a": 1}
    assert parse_json_response('{"a": 1}') == {"a": 1}


# --- retry / backoff -------------------------------------------------------


def test_retries_after_api_error_then_succeeds():
    sleeps = []
    call = fake_call([RuntimeError("rate limited"), good_json()])
    result = run(call, sleep=sleeps.append)
    assert result.attempts == 2
    assert len(sleeps) == 1 and sleeps[0] >= 2.0


def test_raises_last_api_error_when_never_succeeds():
    call = fake_call([RuntimeError("down")] * 2)
    with pytest.raises(RuntimeError, match="down"):
        run(call, max_attempts=2)


def test_retries_on_bad_json_and_schema_error():
    call = fake_call(["not json", {"single_post": "x"}, good_json()])
    result = run(call)
    assert result.attempts == 3


# --- 280 rule --------------------------------------------------------------


def test_280_rule_counts_url_as_23():
    # 257 x + space + long URL: real length far over 280, t.co length exactly 280 -> OK
    ok = "x" * 256 + " " + URL
    draft = validate_output(good_json(single_post=ok))
    assert check_hard_rules(draft, url=URL, source="pubmed") == []
    too_long = "x" * 257 + " " + URL
    draft = validate_output(good_json(single_post=too_long))
    problems = check_hard_rules(draft, url=URL, source="pubmed")
    assert any("single_post is 281 chars" in p for p in problems)


def test_280_rule_applies_to_every_thread_post():
    thread = ["y" * 281, "ok", f"end {URL}"]
    draft = validate_output(good_json(thread=thread))
    problems = check_hard_rules(draft, url=URL, source="pubmed")
    assert any("thread[0] is 281 chars" in p for p in problems)


def test_over_280_draft_is_rejected_after_retries(caplog):
    call = fake_call([good_json(single_post="z" * 300 + " " + URL)] * 2)
    with pytest.raises(DraftRejected) as exc:
        run(call, max_attempts=2)
    assert any("> 280" in r for r in exc.value.reasons)
    assert "draft rejected" in caplog.text


# --- URL rule --------------------------------------------------------------


def test_missing_url_in_single_post_and_last_thread_post():
    draft = validate_output(
        good_json(single_post="no link here", thread=["a", "b", "c without url"])
    )
    problems = check_hard_rules(draft, url=URL, source="pubmed")
    assert "single_post is missing the primary source URL" in problems
    assert "last thread post is missing the primary source URL" in problems


# --- preprint rule ---------------------------------------------------------


@pytest.mark.parametrize("source", ["biorxiv", "medRxiv"])
def test_preprint_must_be_labelled(source):
    draft = validate_output(good_json())
    problems = check_hard_rules(draft, url=URL, source=source)
    assert "preprint not labelled in single_post" in problems
    assert "preprint not labelled in first thread post" in problems

    labelled = good_json(
        single_post=f"Preprint: ORR 88% in 97 patients. {URL}",
        thread=[f"New preprint. {URL}", "b", f"c {URL}"],
    )
    assert check_hard_rules(validate_output(labelled), url=URL, source=source) == []


def test_preprint_label_not_required_for_pubmed():
    draft = validate_output(good_json())
    assert check_hard_rules(draft, url=URL, source="pubmed") == []


# --- medical advice rule ---------------------------------------------------


def test_medical_advice_is_rejected():
    draft = validate_output(
        good_json(single_post=f"Patients should ask their oncologist about this CAR-T. {URL}")
    )
    problems = check_hard_rules(draft, url=URL, source="pubmed")
    assert any("medical advice" in p for p in problems)


# --- number verification ---------------------------------------------------


def test_numbers_in_extracts_and_ignores_urls():
    assert numbers_in("ORR 88%, PFS 14.6 mo, n=1,200 https://doi.org/10.1000/x9") == [
        "88%",
        "14.6",
        "1,200",
    ]


def test_verify_numbers_flags_numbers_absent_from_abstract():
    draft = validate_output(good_json())
    assert verify_numbers(draft, ABSTRACT) == []
    draft = validate_output(good_json(single_post=f"ORR 88% and PFS 15 months, n=100. {URL}"))
    assert verify_numbers(draft, ABSTRACT) == ["15", "100"]


def test_percent_must_be_percent_in_source():
    # '4' appears in the abstract but '4%' does not (it says 4.1%)
    draft = validate_output(good_json(single_post=f"CRS in 4% of patients {URL}"))
    assert verify_numbers(draft, ABSTRACT) == ["4%"]


def test_unverified_numbers_become_low_confidence_claims():
    call = fake_call([good_json(single_post=f"ORR 88% and PFS 15 months. {URL}")])
    result = run(call)
    assert result.flagged_numbers == ["15"]
    claims = result.draft.claims_to_verify
    assert len(claims) == 1
    assert claims[0].confidence == "low" and "'15'" in claims[0].claim
    # idempotent
    flag_unverified_numbers(result.draft, ABSTRACT)
    assert len(result.draft.claims_to_verify) == 1


# --- step 7: examples never weaken the hard rules ----------------------------


EXAMPLES = (
    "=== RECENT HUMAN EDITS ===\n--- Edit 1 ---\nBEFORE (model):\nold text\n"
    f"AFTER (human):\nPatients should ask their oncologist. {URL}\nWHY: taste"
)


def test_examples_block_reaches_system_prompt_only():
    call = fake_call([good_json()])
    result = run(call, examples_block=EXAMPLES)
    assert result.attempts == 1
    system, user, _ = call.calls[0]
    assert EXAMPLES in system and EXAMPLES not in user
    assert system.index(EXAMPLES) < system.index("HARD RULES")


def test_hard_rules_still_enforced_with_examples_present():
    advice = good_json(single_post=f"Patients should ask their oncologist about this. {URL}")
    call = fake_call([advice, advice])
    with pytest.raises(DraftRejected) as exc:
        run(call, examples_block=EXAMPLES, max_attempts=2)
    assert any("medical advice" in r for r in exc.value.reasons)
    assert all(EXAMPLES in c[0] for c in call.calls)


def test_existing_call_signature_without_examples_still_works():
    call = fake_call([good_json()])
    assert run(call).attempts == 1
    assert "RECENT HUMAN EDITS" not in call.calls[0][0]


# --- step 7: model resolution -------------------------------------------------


def test_model_name_env_wins_then_root_config_then_draft_config(monkeypatch):
    import config as root_config
    from draft import drafter
    from draft.settings import load_draft_config

    fallback = load_draft_config()["model"]
    monkeypatch.delenv("DRAFT_MODEL", raising=False)

    monkeypatch.setattr(root_config, "load_config", lambda *a, **k: {"models": {"drafter": "x"}})
    assert drafter.model_name() == "x"

    monkeypatch.setattr(root_config, "load_config", lambda *a, **k: {"models": {"scorer": "s"}})
    assert drafter.model_name() == fallback

    monkeypatch.setattr(root_config, "load_config", lambda *a, **k: {})
    assert drafter.model_name() == fallback

    monkeypatch.setenv("DRAFT_MODEL", "from-env")
    monkeypatch.setattr(root_config, "load_config", lambda *a, **k: {"models": {"drafter": "x"}})
    assert drafter.model_name() == "from-env"


def test_model_name_survives_unreadable_root_config(monkeypatch):
    import config as root_config
    from draft import drafter
    from draft.settings import load_draft_config

    monkeypatch.delenv("DRAFT_MODEL", raising=False)

    def boom(*a, **k):
        raise FileNotFoundError("no config.yaml")

    monkeypatch.setattr(root_config, "load_config", boom)
    assert drafter.model_name() == load_draft_config()["model"]


def test_no_claude_model_literal_in_drafter():
    import re
    from pathlib import Path

    import draft.drafter as mod

    src = Path(mod.__file__).read_text(encoding="utf-8")
    assert not re.search(r"claude-", src), "model IDs belong in config, not draft/drafter.py"
