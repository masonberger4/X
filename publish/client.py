"""The only module that talks to X. tweepy is imported inside the functions, never at import.

post_tweet (v2 create_tweet) and upload_media (v1.1 media/upload + alt text) are the two
write calls; verify_credentials is the read check.

Keys come from the environment (.env via python-dotenv, loaded by the CLI):
  X_API_KEY, X_API_SECRET, X_ACCESS_TOKEN, X_ACCESS_SECRET (X_ACCESS_TOKEN_SECRET also accepted)
"""

from __future__ import annotations

import logging
import os
import time
from collections.abc import Callable
from typing import Any

log = logging.getLogger(__name__)

MAX_ATTEMPTS = 4
BACKOFF_BASE_SECONDS = 2.0


class PublishError(RuntimeError):
    """An X API call failed for good (after retries, or on a non-retryable status)."""

    def __init__(self, message: str, *, retryable: bool = False, status: int | None = None):
        super().__init__(message)
        self.retryable = retryable
        self.status = status


def _keys() -> dict[str, str]:
    env = os.environ
    keys = {
        "consumer_key": env.get("X_API_KEY", ""),
        "consumer_secret": env.get("X_API_SECRET", ""),
        "access_token": env.get("X_ACCESS_TOKEN", ""),
        "access_token_secret": env.get("X_ACCESS_SECRET") or env.get("X_ACCESS_TOKEN_SECRET", ""),
    }
    missing = [k for k, v in keys.items() if not v]
    if missing:
        raise PublishError(f"missing X credentials: {', '.join(missing)}")
    return keys


def _client():
    import tweepy  # imported here so tests never need it

    return tweepy.Client(**_keys())


def _api_v1():
    """Media upload is still a v1.1 endpoint; same OAuth 1.0a user keys as the v2 client."""
    import tweepy

    k = _keys()
    auth = tweepy.OAuth1UserHandler(
        k["consumer_key"], k["consumer_secret"], k["access_token"], k["access_token_secret"]
    )
    return tweepy.API(auth)


def _classify(exc: Exception) -> tuple[bool, int | None]:
    """(retryable?, http status) for a tweepy exception. 429/5xx retry; 401/403 never."""
    import tweepy

    if isinstance(exc, tweepy.TooManyRequests):
        return True, 429
    if isinstance(exc, tweepy.TwitterServerError):
        return True, getattr(getattr(exc, "response", None), "status_code", 500)
    if isinstance(exc, tweepy.Unauthorized):
        return False, 401
    if isinstance(exc, tweepy.Forbidden):
        return False, 403
    return False, getattr(getattr(exc, "response", None), "status_code", None)


def _with_retries(op: Callable[[], Any], what: str, sleep: Callable[[float], None] = time.sleep):
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            return op()
        except Exception as exc:  # noqa: BLE001 - classified below
            retryable, status = _classify(exc)
            msg = f"{what}: HTTP {status}: {str(exc)[:200]}"
            if not retryable or attempt == MAX_ATTEMPTS:
                log.error("%s (giving up%s)", msg, "" if not retryable else f" after {attempt}")
                raise PublishError(msg, retryable=retryable, status=status) from exc
            delay = BACKOFF_BASE_SECONDS * 2 ** (attempt - 1)
            log.warning("%s; retry %d/%d in %.0fs", msg, attempt, MAX_ATTEMPTS - 1, delay)
            sleep(delay)
    raise AssertionError("unreachable")


def post_tweet(
    text: str, in_reply_to: str | None = None, media_ids: list[str] | None = None
) -> str:
    """Post one tweet and return its id. media_ids come from upload_media(). Raises
    PublishError on failure."""

    def op():
        kwargs: dict[str, Any] = {"text": text}
        if in_reply_to:
            kwargs["in_reply_to_tweet_id"] = in_reply_to
        if media_ids:
            kwargs["media_ids"] = list(media_ids)
        resp = _client().create_tweet(**kwargs)
        return str(resp.data["id"])

    return _with_retries(op, "create_tweet")


def upload_media(path: str, alt_text: str = "") -> str:
    """Upload one image and return its media_id string, with alt text set when given.
    Raises PublishError on failure (nothing was tweeted yet, so the caller can stop)."""

    def op():
        api = _api_v1()
        media = api.media_upload(filename=path)
        media_id = str(media.media_id)
        if alt_text:
            api.create_media_metadata(media_id, alt_text)
        return media_id

    return _with_retries(op, "media_upload")


def verify_credentials() -> str:
    """Return the authenticated username. Raises PublishError if the keys do not work."""

    def op():
        resp = _client().get_me()
        return str(resp.data["username"])

    return _with_retries(op, "get_me")
