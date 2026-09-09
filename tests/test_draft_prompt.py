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
