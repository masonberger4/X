"""The ONLY module that talks to the X API, read-only, via httpx with app-only (bearer) auth.

  get_tweet_metrics(tweet_ids) -> dict[tweet_id, TweetMetrics]
      GET /2/tweets?ids=...&tweet.fields=public_metrics,created_at, <= 100 ids per call.
      Ids missing from the response (deleted, suspended, protected) come back with
      deleted=True; nothing is raised for them.
  get_user_metrics(username) -> UserMetrics
      GET /2/users/by/username/:username?user.fields=public_metrics

Retries 429 (honouring x-rate-limit-reset, capped by rate_limit.max_wait_seconds) and 5xx
with exponential backoff; never retries 401/403. The bearer token is never logged.
"""

from __future__ import annotations

import logging
import os
import time
from collections.abc import Callable, Iterable
from datetime import UTC, datetime
from typing import Any

import httpx

from feedback.models import Metrics, TweetMetrics, UserMetrics

log = logging.getLogger(__name__)

DEFAULT_API_BASE = "https://api.x.com/2"
DEFAULT_TOKEN_ENV = "X_BEARER_TOKEN"
MAX_IDS_PER_CALL = 100
TWEET_FIELDS = "public_metrics,created_at"
USER_FIELDS = "public_metrics"

_METRIC_KEYS = {
    "impressions": "impression_count",
    "likes": "like_count",
    "reposts": "retweet_count",
    "replies": "reply_count",
    "quotes": "quote_count",
    "bookmarks": "bookmark_count",
}

# Patchable in tests so retry tests do not sleep.
_sleep: Callable[[float], None] = time.sleep


class FeedbackAPIError(RuntimeError):
    """An X API call failed for good (after retries, or on a non-retryable status)."""

    def __init__(self, message: str, *, status: int | None = None, retryable: bool = False):
        super().__init__(message)
        self.status = status
        self.retryable = retryable


class RetryPolicy:
    def __init__(self, cfg: dict[str, Any] | None = None):
        c = (cfg or {}).get("rate_limit") or {}
        self.max_attempts = max(1, int(c.get("max_attempts", 4)))
        self.backoff_seconds = float(c.get("backoff_seconds", 2))
        self.max_wait_seconds = float(c.get("max_wait_seconds", 900))


def _token(cfg: dict[str, Any] | None) -> str:
    env_name = (cfg or {}).get("bearer_token_env") or DEFAULT_TOKEN_ENV
    token = os.environ.get(env_name, "")
    if not token:
        raise FeedbackAPIError(f"missing X bearer token: set {env_name} in .env")
    return token


def _api_base(cfg: dict[str, Any] | None) -> str:
    return str((cfg or {}).get("api_base") or DEFAULT_API_BASE).rstrip("/")


def _wait_for_429(resp: httpx.Response, attempt: int, policy: RetryPolicy) -> float:
    """Seconds to wait: x-rate-limit-reset (epoch seconds) if present, else backoff; capped."""
    reset = resp.headers.get("x-rate-limit-reset")
    wait = policy.backoff_seconds * (2**attempt)
    if reset:
        try:
            wait = max(1.0, float(reset) - time.time() + 1.0)
        except ValueError:
            pass
    return min(wait, policy.max_wait_seconds)


def _get(url: str, params: dict[str, str], *, cfg: dict[str, Any] | None = None) -> dict[str, Any]:
    """GET with bearer auth and the retry policy. Returns the parsed JSON body."""
    policy = RetryPolicy(cfg)
    headers = {"Authorization": f"Bearer {_token(cfg)}"}
    last_error = "no attempts made"
    for attempt in range(policy.max_attempts):
        try:
            with httpx.Client(timeout=30.0) as client:
                resp = client.get(url, params=params, headers=headers)
        except httpx.HTTPError as exc:  # network trouble: retryable
            last_error = f"{type(exc).__name__}: {exc}"
            log.warning("X API request failed (%s); attempt %d", last_error, attempt + 1)
            _sleep(min(policy.backoff_seconds * (2**attempt), policy.max_wait_seconds))
            continue
        status = resp.status_code
        if status == 200:
            return resp.json()
        if status in (401, 403):
            log.error("X API returned %d for %s; not retrying", status, url)
            raise FeedbackAPIError(f"HTTP {status} from X API", status=status)
        if status == 429:
            wait = _wait_for_429(resp, attempt, policy)
            last_error = "HTTP 429"
            log.warning("X API rate limited; waiting %.0fs (attempt %d)", wait, attempt + 1)
            _sleep(wait)
            continue
        if status >= 500:
            wait = min(policy.backoff_seconds * (2**attempt), policy.max_wait_seconds)
            last_error = f"HTTP {status}"
            log.warning("X API returned %d; retrying in %.0fs", status, wait)
            _sleep(wait)
            continue
        log.error("X API returned %d for %s", status, url)
        raise FeedbackAPIError(f"HTTP {status} from X API", status=status)
    log.error("X API gave up after %d attempts: %s", policy.max_attempts, last_error)
    raise FeedbackAPIError(
        f"gave up after {policy.max_attempts} attempts: {last_error}", retryable=True
    )


# ---------------------------------------------------------------------------
# Parsing (pure; tested against a saved fixture)
# ---------------------------------------------------------------------------


def _parse_created(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(UTC)
    except ValueError:
        return None


def parse_tweets_response(
    payload: dict[str, Any], requested: Iterable[str]
) -> dict[str, TweetMetrics]:
    """Map every requested id to TweetMetrics; ids absent from `data` are deleted=True."""
    out: dict[str, TweetMetrics] = {}
    for item in payload.get("data") or []:
        tid = str(item.get("id", ""))
        pm = item.get("public_metrics") or {}
        out[tid] = TweetMetrics(
            tweet_id=tid,
            metrics=Metrics(**{k: int(pm.get(v, 0) or 0) for k, v in _METRIC_KEYS.items()}),
            created_at=_parse_created(item.get("created_at")),
        )
    missing = [str(t) for t in requested if str(t) not in out]
    errors = {str(e.get("resource_id")): e for e in payload.get("errors") or []}
    for tid in missing:
        err = errors.get(tid)
        log.info(
            "tweet %s not returned (%s); marking deleted",
            tid,
            err.get("title") if err else "absent",
        )
        out[tid] = TweetMetrics(tweet_id=tid, deleted=True)
    return out


def parse_user_response(payload: dict[str, Any], username: str) -> UserMetrics:
    data = payload.get("data") or {}
    if not data:
        errs = payload.get("errors") or []
        detail = errs[0].get("title") if errs else "no data"
        raise FeedbackAPIError(f"user {username!r} not returned: {detail}")
    pm = data.get("public_metrics") or {}
    return UserMetrics(
        username=str(data.get("username") or username),
        followers=int(pm.get("followers_count", 0) or 0),
        following=int(pm.get("following_count", 0) or 0),
        tweet_count=int(pm.get("tweet_count", 0) or 0),
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def get_tweet_metrics(
    tweet_ids: Iterable[str], cfg: dict[str, Any] | None = None
) -> dict[str, TweetMetrics]:
    ids = list(dict.fromkeys(str(t) for t in tweet_ids))
    batch = int(((cfg or {}).get("snapshot") or {}).get("batch_size", MAX_IDS_PER_CALL))
    batch = max(1, min(batch, MAX_IDS_PER_CALL))
    out: dict[str, TweetMetrics] = {}
    for start in range(0, len(ids), batch):
        chunk = ids[start : start + batch]
        payload = _get(
            f"{_api_base(cfg)}/tweets",
            {"ids": ",".join(chunk), "tweet.fields": TWEET_FIELDS},
            cfg=cfg,
        )
        out.update(parse_tweets_response(payload, chunk))
    return out


def get_user_metrics(username: str, cfg: dict[str, Any] | None = None) -> UserMetrics:
    username = username.lstrip("@")
    payload = _get(
        f"{_api_base(cfg)}/users/by/username/{username}", {"user.fields": USER_FIELDS}, cfg=cfg
    )
    return parse_user_response(payload, username)
