"""thread.py: splitting, the 280 rule (URLs = 23), numbering."""

import json

import pytest

from publish.thread import ThreadError, check_post, number_posts, parse_thread_json, split_thread

URL = "https://www.nejm.org/doi/full/10.1056/NEJMoa2026001234567890"  # much longer than 23


def test_parse_thread_json_validates():
    assert parse_thread_json(json.dumps([" a ", "b"])) == ["a", "b"]
    for bad in ("not json", "[]", '["a", ""]', '["a", 1]', '{"a": 1}'):
        with pytest.raises(ThreadError):
            parse_thread_json(bad)


def test_check_post_counts_urls_as_23():
    text = "x" * 257 + " " + URL  # 257 + 1 + 23 = 281
    assert check_post(text) == ["281 chars > 280"]
    assert check_post("x" * 256 + " " + URL) == []


def test_split_thread_numbers_and_keeps_order():
    posts = ["first", "second", "third"]
    out = split_thread(json.dumps(posts))
    assert out == ["first (1/3)", "second (2/3)", "third (3/3)"]


def test_the_opening_post_can_be_left_unnumbered():
    """thread_numbering: replies (the shipped value) keeps the marker rule 12 bans off the
    head, which is also what the drafter's hook check passed."""
    from draft.hook import hook_problems

    out = split_thread(["The armoring worked in blood.", "second", "third"], number_first=False)
    assert out == ["The armoring worked in blood.", "second (2/3)", "third (3/3)"]
    assert hook_problems(out[0]) == []
    assert number_posts(["a", "b"], first=False) == ["a", "b (2/2)"]


def test_numbering_skipped_when_it_would_not_fit():
    long = "y" * 278
    posts = [long, f"end {URL}"]
    out = number_posts(posts)
    assert out[0] == long  # unchanged, no room for " (1/2)"
    assert out[1] == f"end {URL} (2/2)"
    assert number_posts(["solo"]) == ["solo"]


def test_split_thread_refuses_over_280_instead_of_trimming():
    posts = ["z" * 281, "end"]
    with pytest.raises(ThreadError, match="post 1: 281 chars"):
        split_thread(posts)


def test_split_thread_never_asks_for_a_url():
    # no post carries a link any more, and a human-approved text is never edited here
    assert split_thread(["lead", "middle", "end"]) == ["lead (1/3)", "middle (2/3)", "end (3/3)"]
    out = split_thread(["a", f"b {URL}"])
    assert URL in out[-1]


def test_thread_numbering_setting_is_normalised():
    from publish.scheduler import numbering_mode

    assert numbering_mode(False) == "none"  # YAML reads `off` / `no` as False
    assert numbering_mode(" ALL ") == "all" and numbering_mode("None") == "none"
    assert numbering_mode("bogus") == "replies"  # safe default for rule 12
    assert numbering_mode(None) == "replies"  # a blank `thread_numbering:` is the default
