"""thread.py: splitting, the 280 rule (URLs = 23), numbering, URL in last post."""

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
    assert check_post("no link here", url=URL) == ["missing source URL"]


def test_split_thread_numbers_and_keeps_order():
    posts = ["first", "second", f"third {URL}"]
    out = split_thread(json.dumps(posts), url=URL)
    assert out == ["first (1/3)", "second (2/3)", f"third {URL} (3/3)"]


def test_numbering_skipped_when_it_would_not_fit():
    long = "y" * 278
    posts = [long, f"end {URL}"]
    out = number_posts(posts)
    assert out[0] == long  # unchanged, no room for " (1/2)"
    assert out[1] == f"end {URL} (2/2)"
    assert number_posts(["solo"]) == ["solo"]


def test_split_thread_refuses_over_280_instead_of_trimming():
    posts = ["z" * 281, f"end {URL}"]
    with pytest.raises(ThreadError, match="post 1: 281 chars"):
        split_thread(posts, url=URL)


def test_split_thread_requires_url_in_last_post():
    with pytest.raises(ThreadError, match="last post: missing source URL"):
        split_thread([f"lead {URL}", "middle", "end without link"], url=URL)
    # URL present in the last post is never stripped, even when numbering is added
    out = split_thread(["a", f"b {URL}"], url=URL)
    assert URL in out[-1]
