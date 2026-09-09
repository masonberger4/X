from draft.prompt import HARD_RULES, build_prompt, is_preprint, load_voice_guide


def test_voice_guide_loads_with_required_sections():
    guide = load_voice_guide()
    for section in ("## Tone", "## Sample posts", "## Banned phrases", "## The originality rule"):
        assert section in guide


def test_prompt_embeds_voice_guide_hard_rules_and_item():
    system, user = build_prompt(
        title="A CAR-T trial",
        abstract="ORR was 88%.",
        url="https://example.org/paper",
        source="pubmed",
        suggested_angle="compare with bispecifics",
        rationale="high novelty",
    )
    assert "VOICE GUIDE" in system
    assert HARD_RULES in system
    assert "No medical advice" in system
    assert '"single_post"' in system  # JSON schema embedded
    assert "A CAR-T trial" in user
    assert "ORR was 88%." in user
    assert "https://example.org/paper" in user
    assert "compare with bispecifics" in user
    assert "high novelty" in user
    assert "PREPRINT" not in user


def test_preprint_flagged_in_user_prompt():
    _, user = build_prompt(title="t", abstract="a", url="https://x.y", source="biorxiv")
    assert "THIS IS A PREPRINT" in user
    assert is_preprint("medRxiv")
    assert is_preprint("biorxiv_cancer_biology") and is_preprint("medrxiv_oncology")
    assert not is_preprint("pubmed")
    assert not is_preprint(None)


def test_missing_abstract_does_not_crash():
    _, user = build_prompt(title="t", abstract="", url="https://x.y", source="fda")
    assert "no abstract available" in user


# --- step 7: examples block ----------------------------------------------------

ITEM = dict(title="A CAR-T trial", abstract="ORR was 88%.", url="https://x.y/p", source="pubmed")


def _pre_step7_system_prompt() -> str:
    """The system prompt exactly as build_system_prompt() produced it before step 7."""
    import json

    from draft.schema import OUTPUT_JSON_SCHEMA

    return (
        "You draft posts for an X account on the business and investing side of "
        "immuno-oncology biotech, written as a PhD-level immuno-oncology analyst at a hedge "
        "fund would write them. A human reviews and edits every draft before anything is "
        "published; nothing you write is posted automatically.\n\n"
        "Follow the voice guide exactly.\n\n"
        "=== VOICE GUIDE ===\n"
        f"{load_voice_guide()}\n"
        "=== END VOICE GUIDE ===\n\n"
        f"{HARD_RULES}\n"
        "Respond with a single JSON object and nothing else, matching this JSON schema:\n"
        f"{json.dumps(OUTPUT_JSON_SCHEMA, indent=2)}"
    )


def test_prompt_without_examples_is_byte_identical_to_before():
    from draft.prompt import build_system_prompt

    assert build_prompt(**ITEM) == build_prompt(**ITEM, examples_block=None)
    assert build_prompt(**ITEM, examples_block="") == build_prompt(**ITEM)
    assert build_system_prompt() == _pre_step7_system_prompt()
    assert build_prompt(**ITEM)[0] == _pre_step7_system_prompt()


def test_examples_block_sits_after_voice_guide_and_before_hard_rules():
    block = "=== RECENT HUMAN EDITS ===\nBEFORE (model):\nold\nAFTER (human):\nnew\nWHY: hype"
    system, user = build_prompt(**ITEM, examples_block=block)
    assert block in system
    assert block not in user  # the block lives in the system prompt only
    assert system.index("=== END VOICE GUIDE ===") < system.index(block) < system.index(HARD_RULES)
    assert system.index(HARD_RULES) < system.index('"single_post"')
    # everything outside the block is unchanged
    assert system.replace(block + "\n\n", "") == _pre_step7_system_prompt()
