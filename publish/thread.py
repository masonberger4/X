"""Turn a draft thread into the ordered list of texts that will be posted.

The only text change allowed here is appending " (n/N)" to a post, and only when it still
fits; the opening post is left as approved unless `thread_numbering: all` asks otherwise,
since rule 12 (draft/hook.py) keeps a position marker off it. Anything else (trimming,
rewording) belongs in the approval queue.
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


def check_post(text: str, *, max_chars: int = MAX_POST_CHARS) -> list[str]:
    """Problems with one post: over the limit (280, or the long post's `max_chars`; a URL
    counts 23, though no drafted post carries one)."""
    problems: list[str] = []
    n = tweet_length(text)
    if n > max_chars:
        problems.append(f"{n} chars > {max_chars}")
    return problems


NUMBERING = ("replies", "all", "none")  # publish/config.yaml thread_numbering


def number_posts(posts: list[str], *, first: bool = True) -> list[str]:
    """Append ' (n/N)' to each post if it still fits; otherwise leave that post unnumbered.
    `first` False leaves the opening post as it is: it is the one post X shows people who
    do not follow the account, and rule 12 keeps a position marker off it."""
    total = len(posts)
    if total < 2:
        return list(posts)
    out = []
    for i, p in enumerate(posts, 1):
        suffix = f" ({i}/{total})"
        if i == 1 and not first:
            out.append(p)
            continue
        out.append(p + suffix if tweet_length(p + suffix) <= MAX_POST_CHARS else p)
    return out


def split_thread(
    thread_json: str | list[str],
    *,
    max_chars: int = MAX_POST_CHARS,
    number: bool = True,
    number_first: bool = True,
) -> list[str]:
    """Ordered posts ready to send. Raises ThreadError rather than editing content.
    `max_chars` is the per-post limit the draft was written to (a long post's, phase
    four). `number` is False for a single or long post, which is not a thread a reader
    counts through; `number_first` False keeps the marker off the opening post."""
    posts = thread_json if isinstance(thread_json, list) else parse_thread_json(thread_json)
    problems = []
    for i, p in enumerate(posts, 1):
        for prob in check_post(p, max_chars=max_chars):
            problems.append(f"post {i}: {prob}")
    if problems:
        raise ThreadError("; ".join(problems))
    return number_posts(posts, first=number_first) if number else list(posts)
