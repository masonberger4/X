"""Turn a draft thread into the ordered list of texts that will be posted.

The only text change allowed here is appending " (n/N)" to a post, and only when it still
fits. Anything else (trimming, rewording, adding a URL) belongs in the approval queue.
"""

from __future__ import annotations

import json

from draft.schema import MAX_POST_CHARS, tweet_length


class ThreadError(ValueError):
    """The thread cannot be posted as-is; the human must fix it in the queue."""


def parse_thread_json(thread_json: str) -> list[str]:
    try:
        data = json.loads(thread_json or "[]")
    except json.JSONDecodeError as exc:
        raise ThreadError(f"thread_json is not valid JSON: {exc}") from exc
    if not isinstance(data, list) or not all(isinstance(p, str) for p in data):
        raise ThreadError("thread_json must be a JSON list of strings")
    posts = [p.strip() for p in data]
    if not posts or any(not p for p in posts):
        raise ThreadError("thread is empty or contains an empty post")
    return posts


def check_post(text: str, *, url: str | None = None) -> list[str]:
    """Problems with one post: over 280 (URLs count 23) or missing a required URL."""
    problems: list[str] = []
    n = tweet_length(text)
    if n > MAX_POST_CHARS:
        problems.append(f"{n} chars > {MAX_POST_CHARS}")
    if url and url not in text:
        problems.append("missing source URL")
    return problems


def number_posts(posts: list[str]) -> list[str]:
    """Append ' (n/N)' to each post if it still fits; otherwise leave that post unnumbered."""
    total = len(posts)
    if total < 2:
        return list(posts)
    out = []
    for i, p in enumerate(posts, 1):
        suffix = f" ({i}/{total})"
        out.append(p + suffix if tweet_length(p + suffix) <= MAX_POST_CHARS else p)
    return out


def split_thread(thread_json: str | list[str], *, url: str) -> list[str]:
    """Ordered posts ready to send. Raises ThreadError rather than editing content."""
    posts = thread_json if isinstance(thread_json, list) else parse_thread_json(thread_json)
    problems = []
    for i, p in enumerate(posts, 1):
        for prob in check_post(p):
            problems.append(f"post {i}: {prob}")
    if url not in posts[-1]:
        problems.append("last post: missing source URL")
    if problems:
        raise ThreadError("; ".join(problems))
    return number_posts(posts)
