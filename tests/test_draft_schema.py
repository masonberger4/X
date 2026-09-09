import pytest

from draft.schema import (
    MAX_POST_CHARS,
    Claim,
    Draft,
    SchemaError,
    tweet_length,
    validate_output,
)


def good_output(**overrides):
    data = {
        "single_post": "ORR 88% in a single-arm trial. Watch for OS. https://example.org/x",
        "thread": ["one", "two", "three https://example.org/x"],
        "suggested_visual": "swimmer plot",
        "why_it_matters": "Single-arm data cannot answer sequencing.",
        "claims_to_verify": [{"claim": "ORR 88%", "confidence": "high"}],
    }
    data.update(overrides)
    return data


def test_validate_good_output():
    draft = validate_output(good_output())
    assert isinstance(draft, Draft)
    assert draft.thread == ["one", "two", "three https://example.org/x"]
    assert draft.claims_to_verify == [Claim("ORR 88%", "high")]
    assert draft.to_dict()["single_post"].startswith("ORR")


def test_missing_key():
    data = good_output()
    del data["thread"]
    with pytest.raises(SchemaError, match="missing keys: thread"):
        validate_output(data)


def test_extra_key_rejected():
    with pytest.raises(SchemaError, match="unexpected keys"):
        validate_output(good_output(hashtags=["#x"]))


@pytest.mark.parametrize("thread", [["a", "b"], ["a"] * 7, "not a list", ["a", 2, "c"]])
def test_thread_bounds_and_types(thread):
    with pytest.raises(SchemaError):
        validate_output(good_output(thread=thread))


def test_empty_thread_post_rejected():
    with pytest.raises(SchemaError, match="empty"):
        validate_output(good_output(thread=["a", "  ", "c"]))


def test_bad_confidence():
    with pytest.raises(SchemaError, match="confidence"):
        validate_output(good_output(claims_to_verify=[{"claim": "x", "confidence": "sure"}]))


def test_non_object():
    with pytest.raises(SchemaError):
        validate_output(["not", "an", "object"])


def test_tweet_length_counts_urls_as_23():
    url = "https://www.ncbi.nlm.nih.gov/pmc/articles/PMC1234567/very/long/path?x=1"
    assert len(url) > 23
    assert tweet_length(url) == 23
    assert tweet_length("abc " + url) == 4 + 23
    assert tweet_length("a " + url + " " + url) == 3 + 46
    assert tweet_length("no url here") == 11


def test_tweet_length_exactly_280_is_fine():
    text = "x" * 256 + " https://a.b/c"
    assert tweet_length(text) == MAX_POST_CHARS
