"""The only module that talks to X. tweepy is imported inside the functions, never at import.

Every call is an X API v2 request on tweepy's OAuth 1.0a signing session against api.x.com:
post_tweet (POST /2/tweets) and upload_media (POST /2/media/upload, then /2/media/metadata
for the alt text) are the two write calls; verify_credentials (GET /2/users/me) is the read
check. tweepy.Client is not used: it hardcodes api.twitter.com, which some networks reset,
and it has no v2 media call.

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
API_HOST = "https://api.x.com"  # not api.twitter.com: some networks reset the legacy host
TWEETS_URL = f"{API_HOST}/2/tweets"
ME_URL = f"{API_HOST}/2/users/me"
MEDIA_UPLOAD_URL = f"{API_HOST}/2/media/upload"
MEDIA_METADATA_URL = f"{API_HOST}/2/media/metadata"
MEDIA_CATEGORY = "tweet_image"
REQUEST_TIMEOUT_SECONDS = 60
TRANSPORT_PREFIX = "Failed to send request"  # tweepy's own wording for a transport error


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


def _oauth1_api():
    """A tweepy.API for its OAuth 1.0a signing session; the v2 media calls go through it."""
    import tweepy

    k = _keys()
    auth = tweepy.OAuth1UserHandler(
        k["consumer_key"], k["consumer_secret"], k["access_token"], k["access_token_secret"]
    )
    return tweepy.API(auth)


def _http_error(resp: Any) -> Exception:
    """The tweepy exception for a v2 response status, so _classify treats both paths alike."""
    import tweepy

    code = resp.status_code
    if code == 400:
        return tweepy.BadRequest(resp)
    if code == 401:
        return tweepy.Unauthorized(resp)
    if code == 403:
        return tweepy.Forbidden(resp)
    if code == 404:
        return tweepy.NotFound(resp)
    if code == 429:
        return tweepy.TooManyRequests(resp)
    if code >= 500:
        return tweepy.TwitterServerError(resp)
    return tweepy.HTTPException(resp)


def _v2_request(api: Any, method: str, url: str, **kwargs: Any) -> dict[str, Any]:
    """One signed v2 call on tweepy's session. Transport errors become a TweepyException with
    tweepy's own prefix (retryable in _classify); HTTP errors become tweepy's status classes."""
    import tweepy

    try:
        resp = api.session.request(
            method, url, auth=api.auth.apply_auth(), timeout=REQUEST_TIMEOUT_SECONDS, **kwargs
        )
    except Exception as exc:  # noqa: BLE001 - requests transport errors, wrapped like tweepy does
        raise tweepy.TweepyException(f"{TRANSPORT_PREFIX}: {exc}") from exc
    if resp.status_code >= 400:
        raise _http_error(resp)
    return resp.json() if resp.content else {}


def _classify(exc: Exception) -> tuple[bool, int | None]:
    """(retryable?, http status) for a tweepy exception. 429/5xx and transport errors retry;
    401/403 never."""
    import tweepy

    if isinstance(exc, tweepy.TooManyRequests):
        return True, 429
    if isinstance(exc, tweepy.TwitterServerError):
        return True, getattr(getattr(exc, "response", None), "status_code", 500)
    if isinstance(exc, tweepy.Unauthorized):
        return False, 401
    if isinstance(exc, tweepy.Forbidden):
        return False, 403
    if isinstance(exc, tweepy.TweepyException) and str(exc).startswith(TRANSPORT_PREFIX):
        return True, None  # tweepy.API wraps a transport error this way
    if isinstance(exc, OSError):
        # requests' ConnectionError/Timeout are OSError and reach us raw from the session.
        # Nothing reached X, or X's answer was lost; in the second case a retried tweet
        # with identical text is refused by X as a duplicate, so this cannot double-post.
        return True, None
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
    """Post one tweet (POST /2/tweets) and return its id. media_ids come from upload_media().
    Raises PublishError on failure."""

    def op():
        body: dict[str, Any] = {"text": text}
        if in_reply_to:
            body["reply"] = {"in_reply_to_tweet_id": in_reply_to}
        if media_ids:
            body["media"] = {"media_ids": [str(m) for m in media_ids]}
        data = _v2_request(_oauth1_api(), "POST", TWEETS_URL, json=body)
        return str(data["data"]["id"])

    return _with_retries(op, "create_tweet")


def upload_media(path: str, alt_text: str = "") -> str:
    """Upload one image through v2 media/upload and return its media id string, with alt text
    set through v2 media/metadata when given. Raises PublishError on failure (nothing was
    tweeted yet, so the caller can stop)."""

    def op():
        api = _oauth1_api()
        with open(path, "rb") as fh:
            data = _v2_request(
                api,
                "POST",
                MEDIA_UPLOAD_URL,
                files={"media": (os.path.basename(path), fh)},
                data={"media_category": MEDIA_CATEGORY},
            )
        media_id = str(data["data"]["id"])
        if alt_text:
            _v2_request(
                api,
                "POST",
                MEDIA_METADATA_URL,
                json={"id": media_id, "metadata": {"alt_text": {"text": alt_text}}},
            )
        return media_id

    return _with_retries(op, "media_upload")


def verify_credentials() -> str:
    """Return the authenticated username (GET /2/users/me). Raises PublishError if the keys
    do not work."""

    def op():
        data = _v2_request(_oauth1_api(), "GET", ME_URL)
        return str(data["data"]["username"])

    return _with_retries(op, "get_me")
