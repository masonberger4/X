"""Rule 13: no colons and no dashes in a post."""

from draft.style import style_problems


def test_plain_sentences_pass():
    assert style_problems("ORR was 88% at 8:30 ET, a 2:1 split. Well-tolerated.") == []


def test_a_colon_fails():
    assert style_problems("Read-across: the comps got a price.")
    assert style_problems("Ends with a colon:")


def test_dashes_fail():
    for text in ("Strong data — but single-arm.", "Strong – data", "a -- b", "a - b"):
        assert style_problems(text), text
