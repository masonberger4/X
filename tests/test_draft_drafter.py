import json

import pytest

from draft import drafter
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


# Every number here is verbatim in ABSTRACT, so the chart passes chart_problems.
CHART = {
    "title": "Phase 2 outcomes",
    "labels": ["ORR", "Grade 3 CRS"],
    "values": [88, 4.1],
    "unit": "%",
    "note": "n=97, single arm",
}


def good_json(lead=None, **overrides):
    """A valid model output. `lead` replaces the first thread post (the one the preprint and
    advice rules look at); the last post keeps the URL."""
    data = {
        "thread": [
            "Phase 2 CAR-T data in relapsed myeloma: ORR 88%, median PFS 14.6 months.",
            "Single-arm, so no comparator. The sequencing question is open.",
            "Grade 3 CRS in 4 patients.",
        ],
        "suggested_visual": "bar chart of ORR and CRS",
        "why_it_matters": "Sequencing vs bispecifics is the real question.",
        "claims_to_verify": [],
        "chart": CHART,
    }
    if lead is not None:
        data["thread"] = [lead, *data["thread"][1:]]
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
    call = fake_call(["not json", {"thread": ["x"]}, good_json()])
    result = run(call)
    assert result.attempts == 3


def test_retries_when_the_visual_is_missing():
    no_visual = good_json(chart=None)
    call = fake_call([no_visual, good_json()])
    result = run(call)
    assert result.attempts == 2
    assert "every draft needs a visual" in call.calls[1][1]


# --- 280 rule --------------------------------------------------------------


def test_280_rule_counts_url_as_23():
    # no post may carry a URL, but where one slipped in the length still counts it as 23
    ok = "x" * 256 + " " + URL
    draft = validate_output(good_json(thread=["a", "b", ok]))
    assert not [p for p in check_hard_rules(draft, url=URL, source="pubmed") if "chars" in p]
    too_long = "x" * 257 + " " + URL
    draft = validate_output(good_json(thread=["a", "b", too_long]))
    problems = check_hard_rules(draft, url=URL, source="pubmed")
    assert any("thread[2] is 281 chars" in p for p in problems)


def test_280_rule_applies_to_every_thread_post():
    thread = ["y" * 281, "ok", "end"]
    draft = validate_output(good_json(thread=thread))
    problems = check_hard_rules(draft, url=URL, source="pubmed")
    assert any("thread[0] is 281 chars" in p for p in problems)


def test_over_280_draft_is_rejected_after_retries(caplog):
    call = fake_call([good_json(thread=["a", "b", "z" * 300 + " " + URL])] * 2)
    with pytest.raises(DraftRejected) as exc:
        run(call, max_attempts=2)
    assert any("> 280" in r for r in exc.value.reasons)
    assert "draft rejected" in caplog.text


# --- link ban (rule 2) -----------------------------------------------------


def test_no_post_may_carry_a_link():
    draft = validate_output(good_json(thread=["a", "b", "c without url"]))
    assert check_hard_rules(draft, url=URL, source="pubmed") == []
    # the source URL is a link like any other, wherever it sits
    draft = validate_output(good_json(thread=["a", f"b {URL}", "c"]))
    problems = check_hard_rules(draft, url=URL, source="pubmed")
    assert any("thread[1] contains a link" in p for p in problems)


def test_a_link_to_anywhere_else_fails_too():
    draft = validate_output(good_json(thread=["a", "b", "see https://example.com/study-2 here"]))
    problems = check_hard_rules(draft, url=URL, source="pubmed")
    assert any("thread[2] contains a link" in p for p in problems)


def test_empty_thread_is_a_hard_rule_failure():
    from draft.schema import Draft

    problems = check_hard_rules(Draft([], "", ""), url=URL, source="pubmed")
    assert problems == ["thread is empty"]


# --- preprint rule ---------------------------------------------------------


@pytest.mark.parametrize("source", ["biorxiv", "medRxiv"])
def test_preprint_must_be_labelled(source):
    draft = validate_output(good_json())
    problems = check_hard_rules(draft, url=URL, source=source)
    assert "preprint not labelled in first thread post" in problems

    labelled = good_json(thread=["New preprint.", "b", "c"])
    assert check_hard_rules(validate_output(labelled), url=URL, source=source) == []


def test_preprint_label_not_required_for_pubmed():
    draft = validate_output(good_json())
    assert check_hard_rules(draft, url=URL, source="pubmed") == []


# --- medical advice rule ---------------------------------------------------


def test_medical_advice_is_rejected():
    draft = validate_output(
        good_json(lead="Patients should ask their oncologist about this CAR-T.")
    )
    problems = check_hard_rules(draft, url=URL, source="pubmed")
    assert any("medical advice" in p for p in problems)


def test_discuss_or_consult_your_doctor_is_medical_advice():
    for lead in (
        "Discuss this with your oncologist before your next visit.",
        "Consult with your doctor about this CAR-T therapy.",
    ):
        draft = validate_output(good_json(lead=f"{lead}"))
        problems = check_hard_rules(draft, url=URL, source="pubmed")
        assert any("medical advice" in p for p in problems), lead


def test_industry_discuss_consult_language_is_not_medical_advice():
    draft = validate_output(
        good_json(
            lead=(
                "The sponsor will discuss trial design with the FDA before the next "
                "readout; investigators should consult the protocol for eligibility."
            )
        )
    )
    problems = check_hard_rules(draft, url=URL, source="pubmed")
    assert not any("medical advice" in p for p in problems)


# --- investment advice rule ------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "Buy $TICK before the readout.",
        "This is a strong buy at these levels.",
        "Investors should sell the stock into the PDUFA.",
        "Price target $40.",
        "The stock will double on approval.",
        "Easy money if the ODAC goes well.",
        "Load up on calls here.",
    ],
)
def test_investment_advice_is_rejected(text):
    draft = validate_output(good_json(lead=f"{text} ORR 88%."))
    problems = check_hard_rules(draft, url=URL, source="pubmed")
    assert any("investment advice" in p for p in problems), text


@pytest.mark.parametrize(
    "text",
    [
        "For $TICK the thesis now rests on durability, not ORR.",
        "The $1.4B price puts a value on mid-stage T-cell engagers.",
        "The stock will trade on PFS today; the label will be decided on OS.",
        "Approval is the base case; the label wording decides the market size.",
        "A dilutive financing after data is the risk to watch.",
    ],
)
def test_thesis_language_is_not_investment_advice(text):
    draft = validate_output(good_json(lead=f"{text} ORR 88%."))
    problems = check_hard_rules(draft, url=URL, source="pubmed")
    assert not any("investment advice" in p for p in problems), text


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
    draft = validate_output(good_json(lead="ORR 88% and PFS 15 months, n=100."))
    assert verify_numbers(draft, ABSTRACT) == ["15", "100"]


def test_percent_must_be_percent_in_source():
    # '4' appears in the abstract but '4%' does not (it says 4.1%)
    draft = validate_output(good_json(lead="CRS in 4% of patients"))
    assert verify_numbers(draft, ABSTRACT) == ["4%"]


def test_number_is_not_verified_as_substring_of_a_larger_number():
    # ABSTRACT has "97 patients"; "9" and "7" must not verify as-is via substring match.
    draft = validate_output(good_json(lead="Enrolled 7 patients."))
    assert verify_numbers(draft, ABSTRACT) == ["7"]


def test_percent_not_verified_by_neighbouring_percentile_word():
    source = "an excellent essay about 88 percentile scores"
    draft = validate_output(good_json(thread=["ORR 88%.", "no comparator", "so what"]))
    assert verify_numbers(draft, source) == ["88%"]


def test_unverified_numbers_become_low_confidence_claims():
    call = fake_call([good_json(lead="ORR 88% and PFS 15 months.")])
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
    advice = good_json(lead="Patients should ask their oncologist about this.")
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


def test_retry_prompt_feeds_violations_back_to_the_model():
    assert drafter.retry_prompt("USER", []) == "USER"
    prompt = drafter.retry_prompt("USER", ["thread[4] is 281 chars (> 280)"])
    assert prompt.startswith("USER\n\n")
    assert "thread[4] is 281 chars" in prompt
    assert "260 characters" in prompt


def test_draft_item_retries_with_the_violation_in_the_prompt():
    seen: list[str] = []

    def call(system, user, model):
        seen.append(user)
        long = "x" * 281
        good = "ok"
        thread = [long if len(seen) == 1 else "ok", good, good]
        return json.dumps(
            {
                "thread": thread,
                "why_it_matters": "w",
                "claims_to_verify": [],
                "suggested_visual": "bars",
                "chart": CHART,
            }
        )

    result = drafter.draft_item(
        title="t",
        abstract=ABSTRACT,
        url=URL,
        source="rss",
        call=call,
        model="m",
        sleep=lambda s: None,
    )
    assert result.attempts == 2
    assert "PREVIOUS ATTEMPT" not in seen[0]
    assert "thread[0] is 281 chars" in seen[1]


# --- revise_item ----------------------------------------------------------------


def _current():
    return validate_output(good_json())


def test_revise_item_sends_current_draft_and_instructions(monkeypatch):
    from draft.drafter import revise_item
    from draft.prompt import ClaimProblem

    call = fake_call([good_json(lead="Shorter. ORR 88%.")])
    res = revise_item(
        current=_current(),
        instructions="make the first post shorter",
        claim_problems=[ClaimProblem("myeloma readout was in 2023", "contradicted", note="2024")],
        title="CAR-T in myeloma",
        abstract=ABSTRACT,
        url=URL,
        source="pubmed",
        model="m",
        call=call,
        sleep=lambda s: None,
    )
    assert res.draft.thread[0].startswith("Shorter.")
    system, user, model = call.calls[0]
    assert model == "m"
    assert "HARD RULES" in system
    assert "make the first post shorter" in user
    assert "myeloma readout was in 2023" in user
    assert good_json()["thread"][0] in user  # the current draft is shown
    assert ABSTRACT in user


def test_revise_item_still_enforces_hard_rules():
    from draft.drafter import revise_item

    bad = good_json(lead="Investors should buy the stock now.")
    good = good_json()
    call = fake_call([bad, good])
    res = revise_item(
        current=_current(),
        instructions="x",
        title="t",
        abstract=ABSTRACT,
        url=URL,
        source="pubmed",
        model="m",
        call=call,
        sleep=lambda s: None,
    )
    assert res.attempts == 2
    assert "investment advice" in call.calls[1][1]


def test_revise_item_rejects_after_all_attempts_and_needs_something_to_do():
    from draft.drafter import revise_item

    bad = good_json(lead="Ask your doctor.")
    kw = dict(
        title="t", abstract=ABSTRACT, url=URL, source="pubmed", model="m", sleep=lambda s: None
    )
    with pytest.raises(DraftRejected):
        revise_item(
            current=_current(), instructions="x", call=fake_call([bad] * 4), max_attempts=4, **kw
        )
    with pytest.raises(ValueError):
        revise_item(current=_current(), instructions="  ", call=fake_call([]), **kw)


# --- caption rule ----------------------------------------------------------


def test_chart_note_addressed_to_the_operator_is_a_hard_rule_failure():
    chart = {**CHART, "note": "n=97; verify each value before posting"}
    draft = validate_output(good_json(chart=chart))
    problems = check_hard_rules(draft, url=URL, source="pubmed")
    assert any("chart note addresses the operator" in p for p in problems)


def test_table_note_addressed_to_the_operator_is_a_hard_rule_failure():
    table = {
        "title": "Approved CD3xBCMA bispecifics",
        "columns": ["Asset", "Mechanism", "Schedule"],
        "rows": [
            ["Tecvayli (J&J)", "CD3xBCMA bispecific", "SC, until progression"],
            ["Elrexfio (Pfizer)", "CD3xBCMA bispecific", "SC, until progression"],
        ],
        "note": "Status as of Sept 2026; verify each cell against current FDA labels "
        "before posting",
    }
    draft = validate_output(good_json(chart=None, table=table))
    problems = check_hard_rules(draft, url=URL, source="pubmed")
    assert any("table note addresses the operator" in p for p in problems)


def test_a_factual_caption_passes():
    chart = {**CHART, "note": "n=97, single arm, investigator-assessed; data cutoff Jan 2026"}
    draft = validate_output(good_json(chart=chart))
    assert check_hard_rules(draft, url=URL, source="pubmed") == []
